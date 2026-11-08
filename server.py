#!/usr/bin/env python3
"""wifi-gate：答对题目才放行设备上网（多题库 + 时间档位）。

启动：         python3 server.py
应急一键放行： python3 server.py --restore-all
管理后台：     http://<NAS内网IP>:8080/admin

答题流程（参考「题了个题」的逐题模式）：
  GET  /api/quiz    返回本机所属题库、可选档位（答 N 题换 M 分钟）、剩余时间
  POST /api/start   选一档开始作答，返回本轮第一题
  POST /api/answer  逐题判卷 → 返回对错、正确答案、解析，再给下一题
  一轮全部答对才放行；有错则整轮重来（同一档位），并计入一次错误
"""

import json
import math
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import admin_api
import router_adapter
import store

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
STATE = os.path.join(BASE, "state")
os.makedirs(STATE, exist_ok=True)

LOG_PATH = os.path.join(STATE, "access.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
GRANTS_PATH = os.path.join(STATE, "grants.json")
SESSION_TTL = 1800
MAC_CACHE_TTL = 120
REBLOCK_INTERVAL = 20

_log_lock = threading.Lock()
_grants_lock = threading.Lock()
_mutex = threading.Lock()
_sessions = {}
_wrong = {}
_mac_cache = {}


def log(line):
    msg = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line)
    with _log_lock:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg)
    print(msg, end="", flush=True)


STORE = store.Store()
ADAPTER = router_adapter.build(STORE.snapshot()["router"], log)


def _reload_adapter():
    """配置变更后重建适配器（密码 / 地址 / 模式都可能变）。"""
    global ADAPTER
    ADAPTER = router_adapter.build(STORE.snapshot()["router"], log)


class NotManaged(Exception):
    """设备未归属任何题库，不调用放行接口。"""


class DailyCap(Exception):
    """今天累计上网时长已达上限。"""


# ---- 放行记录 ----

def load_grants():
    if not os.path.exists(GRANTS_PATH):
        return {}
    try:
        with open(GRANTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_grants(grants):
    """原子写，避免容器被 SIGTERM 打断时留下半截 JSON。"""
    tmp = GRANTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grants, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GRANTS_PATH)


def today():
    return time.strftime("%Y-%m-%d")


def minutes_left(rec):
    """剩余分钟；None 表示已设为永久放行，0 表示已过期/从未放行。"""
    if not rec:
        return 0
    expire = rec.get("expire_at")
    if expire is None:
        return 0
    if expire == 0:
        return None
    left = (int(expire) - time.time()) / 60.0
    return max(0, int(math.ceil(left)))


def granted_today(rec):
    if not rec or rec.get("day") != today():
        return 0
    return int(rec.get("today_minutes", 0) or 0)


# ---- 客户端与设备识别 ----

_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

_lan_ip_cache = {"value": None}


def lan_ip():
    """本机在局域网里的地址，用于自检和运维提示（UDP connect 不发包）。"""
    if _lan_ip_cache["value"] is not None:
        return _lan_ip_cache["value"]
    ip = ""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))
        ip = s.getsockname()[0]
    except OSError:
        ip = ""
    finally:
        s.close()
    _lan_ip_cache["value"] = ip
    return ip


def client_ip(handler):
    """只有本机或显式信任的反向代理才能用 X-Forwarded-For。

    否则局域网里任何设备都能伪造 XFF，答一次题放行别人的设备。
    本机 127.0.0.1 仍可用 XFF 模拟多设备，供 selftest.py 使用。
    """
    peer = handler.client_address[0]
    trust = STORE.trust_x_forwarded_for or peer in _LOOPBACK
    if trust:
        forwarded = handler.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def resolve_mac(ip):
    """IP → MAC，带 2 分钟缓存，避免答题过程中反复拉设备列表。"""
    now = time.time()
    with _mutex:
        hit = _mac_cache.get(ip)
        if hit and now - hit[1] < MAC_CACHE_TTL:
            return hit[0]
    mac = ADAPTER.resolve_mac(ip)
    with _mutex:
        _mac_cache[ip] = (mac, now)
    return mac


def device_context(ip):
    """返回 (mac, 题库或 None, 放行记录)。"""
    mac = resolve_mac(ip)
    bank = STORE.bank_for_mac(mac)
    rec = load_grants().get(mac) or {}
    return mac, bank, rec


# ---- 答题轮次 ----

def public_question(q):
    return {
        "id": q["id"], "text": q["text"], "options": q["options"],
        "type": q["type"], "stage": q.get("stage", ""),
        "grade": q.get("grade", ""), "subject": q.get("subject", ""),
        "star": q.get("star", 2),
    }


def start_round(ip, mac, bank, tier_index):
    tier = bank["tiers"][tier_index]
    pool = bank["questions"]
    n = max(1, min(int(tier["questions"]), len(pool)))
    sess = {
        "bank_id": bank["id"], "bank_name": bank["name"], "mac": mac,
        "tier": tier_index, "minutes": int(tier["minutes"]),
        "questions": random.sample(pool, n),
        "idx": 0, "results": [], "ts": time.time(),
    }
    with _mutex:
        _sessions[ip] = sess
    return sess


def get_round(ip):
    with _mutex:
        sess = _sessions.get(ip)
        if not sess:
            return None
        if time.time() - sess["ts"] > SESSION_TTL:
            _sessions.pop(ip, None)
            return None
        return sess


def round_payload(ip, sess):
    idx = sess["idx"]
    total = len(sess["questions"])
    return {
        "ok": True,
        "ip": ip,
        "mac": sess["mac"],
        "bank": {"id": sess["bank_id"], "name": sess["bank_name"]},
        "tier": sess["tier"],
        "minutes": sess["minutes"],
        "total": total,
        "index": idx,
        "results": sess["results"],
        "question": public_question(sess["questions"][idx]),
        "locked_for": locked_seconds(ip),
    }


def is_correct(q, choices):
    expected = q["answer"] if isinstance(q["answer"], list) else [q["answer"]]
    try:
        got = sorted({int(c) for c in choices})
    except (TypeError, ValueError):
        return False
    return got == sorted(expected)


def locked_seconds(ip):
    cfg = STORE.snapshot()["server"]
    limit = int(cfg["max_wrong_per_window"])
    window = int(cfg["lock_seconds"])
    with _mutex:
        rec = _wrong.get(ip)
        if rec and rec[0] >= limit:
            left = window - (time.time() - rec[1])
            if left > 0:
                return int(left)
            _wrong.pop(ip, None)
    return 0


def note_wrong(ip):
    cfg = STORE.snapshot()["server"]
    limit = int(cfg["max_wrong_per_window"])
    with _mutex:
        rec = _wrong.get(ip) or [0, time.time()]
        rec[0] += 1
        rec[1] = time.time()
        _wrong[ip] = rec
        count = rec[0]
    return count, max(0, limit - count)


# ---- 放行 ----

def grant(ip, mac, bank_id, bank_name, minutes):
    """放行并累计时长：已有剩余时间则叠加，受每日上限约束。"""
    cap = int(STORE.snapshot()["settings"].get("daily_cap_minutes", 0) or 0)
    now = time.time()
    day = today()
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        today_before = granted_today(rec)
        granted = int(minutes)
        if cap > 0 and granted > 0:
            granted = max(0, min(granted, cap - today_before))
            if granted == 0:
                raise DailyCap("今天的 %s 分钟额度已用完，明天再来" % cap)
        ADAPTER.set_wan(mac, True)
        if granted == 0 and int(minutes) == 0:
            expire = 0                      # 档位设为 0 分钟 = 永久放行
        else:
            base = int(rec.get("expire_at") or 0)
            start = base if base > now else now
            expire = int(start + granted * 60)
        rec.update({
            "ip": ip, "mac": mac, "at": now, "expire_at": expire,
            "minutes": granted, "bank": bank_id, "bank_name": bank_name,
            "day": day, "today_minutes": today_before + granted,
            "reblocked": False,
        })
        grants[mac] = rec
        save_grants(grants)
    log("ALLOW ip=%s mac=%s bank=%s +%smin expire=%s"
        % (ip, mac, bank_name, granted, expire or "永久"))
    rec["minutes_left"] = minutes_left(rec)
    return rec


def reblock_worker():
    """到点回收：把超过配额时长的设备重新断网。"""
    while True:
        time.sleep(REBLOCK_INTERVAL)
        try:
            with _grants_lock:
                grants = load_grants()
                now = time.time()
                changed = False
                for mac, rec in grants.items():
                    if rec.get("reblocked"):
                        continue
                    expire = int(rec.get("expire_at") or 0)
                    if not expire or now < expire:
                        continue
                    try:
                        ADAPTER.set_wan(mac, False)
                        rec["reblocked"] = True
                        rec["reblocked_at"] = now
                        changed = True
                        log("REBLOCK mac=%s（配额 %s 分钟用完）"
                            % (mac, rec.get("minutes")))
                    except Exception as exc:
                        log("REBLOCK-FAIL mac=%s err=%s" % (mac, exc))
                if changed:
                    save_grants(grants)
        except Exception as exc:  # 守护线程不能死
            log("REBLOCK-WORKER err=%s" % exc)


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = "wifi-gate/3.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200, set_cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body, ctype, filename=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition",
                             "attachment; filename=%s" % filename)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        path = os.path.join(STATIC, os.path.basename(name))
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, code=302):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _admin(self, method):
        size = int(self.headers.get("Content-Length", "0") or 0) if method == "POST" else 0
        raw = self.rfile.read(size) if size else b""
        path = urlparse(self.path).path
        # 家长常用：孩子连错被锁住了，从后台一键解除（不清题库、不清放行记录）
        if path == "/api/admin/unlock":
            if method != "POST":
                return self._json({"ok": False, "detail": "请用 POST"}, 405)
            if not STORE.check_session(admin_api.cookie_token(self.headers)):
                return self._json({"ok": False, "detail": "请先登录后台"}, 401)
            with _mutex:
                cleared = len(_wrong)
                _wrong.clear()
                _sessions.clear()
                _mac_cache.clear()
            log("ADMIN 清除答题锁定（涉及 %s 个 IP）" % cleared)
            return self._json({"ok": True, "cleared": cleared})
        status, obj, cookie = admin_api.dispatch(
            STORE, ADAPTER, method, self.path, self.headers, raw, client_ip(self))
        if isinstance(obj, admin_api.Raw):
            return self._raw(obj.body, obj.ctype, obj.filename)
        return self._json(obj, status, cookie)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/setup", "/setup.html"):
            # 配置完成后不再暴露引导页，避免有人靠它改写密码
            if STORE.configured:
                return self._redirect("/")
            return self._static("setup.html", "text/html; charset=utf-8")
        if not STORE.configured:
            if path in ("/", "/index.html", "/admin", "/admin.html"):
                return self._redirect("/setup")
            if path.startswith("/api/") and path != "/api/health":
                return self._json({
                    "ok": False, "setup_required": True,
                    "detail": "还没完成初始化，请打开 /setup",
                }, 428)
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path in ("/admin", "/admin.html"):
            return self._static("admin.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._raw(b"", "image/x-icon")
        if path.startswith("/api/admin/"):
            return self._admin("GET")
        if path == "/api/health":
            cfg = STORE.snapshot()
            summary = STORE.summary()
            return self._json({
                "ok": True,
                "configured": STORE.configured,
                "mode": cfg["router"]["mode"],
                "adapter": ADAPTER.describe(),
                "bind": "%s:%s" % (cfg["server"]["host"], cfg["server"]["port"]),
                "trust_xff": STORE.trust_x_forwarded_for,
                "grants": len(load_grants()),
                "banks": summary["banks"],
                "questions": summary["questions"],
                "managed_devices": summary["managed_devices"],
                "daily_cap_minutes": cfg["settings"]["daily_cap_minutes"],
                "lan_ip": lan_ip(),
                "client_ip": client_ip(self),
            })
        if path == "/api/quiz":
            return self._quiz_info()
        return self._json({"error": "not found"}, 404)

    def _quiz_info(self):
        """返回本机所属题库、可选档位与剩余时间（不发题，题由 /api/start 发）。"""
        ip = client_ip(self)
        cfg = STORE.snapshot()
        base = {
            "ok": True, "ip": ip, "note": cfg["settings"]["note"],
            "mode": cfg["router"]["mode"], "locked_for": locked_seconds(ip),
        }
        try:
            mac, bank, rec = device_context(ip)
        except Exception as exc:
            log("MAC-FAIL ip=%s err=%s" % (ip, exc))
            return self._json(dict(base, ok=False, reason="router_error",
                                   detail=str(exc)), 502)
        base["mac"] = mac

        # 可选解锁对象：任何受控设备都能被「代答」放行，
        # 这样家长可以用自己的手机替孩子的平板做题。
        names = ADAPTER.names_by_mac()
        grants = load_grants()
        targets = []
        for t in STORE.targets():
            trec = grants.get(t["mac"]) or {}
            targets.append({
                "mac": t["mac"],
                "name": names.get(t["mac"], ""),
                "bank": {"id": t["bank_id"], "name": t["bank_name"]},
                "tiers": t["tiers"],
                "remaining_minutes": minutes_left(trec),
                "today_minutes": granted_today(trec),
                "is_self": t["mac"] == mac,
            })
        base["targets"] = targets

        if not bank:
            if targets:
                # 本机不受控，但可以替清单里的设备答题（代答）。
                # need 保持 False —— 它描述的是「本机是否需要答题」。
                base.update({"need": False, "pick_target": True,
                             "reason": "pick_target",
                             "message": "这台设备不受控，可以替下面任意一台答题。"})
            else:
                base.update({"need": False, "reason": "not_managed",
                             "message": "这台设备不在受控清单里，不需要答题。"})
            return self._json(base)

        cap = int(cfg["settings"].get("daily_cap_minutes", 0) or 0)
        used = granted_today(rec)
        base.update({
            "need": True,
            "pick_target": False,
            "bank": {"id": bank["id"], "name": bank["name"]},
            "tiers": bank["tiers"],
            "remaining_minutes": minutes_left(rec),
            "today_minutes": used,
            "daily_cap_minutes": cap,
            "today_left": (max(0, cap - used) if cap else None),
        })
        return self._json(base)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/setup":
            return self._setup()
        if not STORE.configured:
            return self._json({
                "ok": False, "setup_required": True,
                "detail": "还没完成初始化，请打开 /setup",
            }, 428)
        if path.startswith("/api/admin/"):
            return self._admin("POST")
        if path == "/api/start":
            return self._start()
        if path != "/api/answer":
            return self._json({"error": "not found"}, 404)
        return self._answer()

    def _setup(self):
        """首次配置：接收路由器密码，写 config.json 并立即生效。"""
        if STORE.configured:
            return self._json({"ok": False, "detail": "已经配置过了"}, 403)
        payload = self._payload()
        if payload is None:
            return self._json({"ok": False, "detail": "请求格式不正确"}, 400)
        try:
            result = STORE.save_config(
                payload.get("router_password"),
                payload.get("admin_password"),
                payload.get("base_url"),
                payload.get("mode") or "miwifi",
                payload.get("dry_run", True),
            )
        except store.StoreError as exc:
            return self._json({"ok": False, "detail": str(exc)}, 400)

        _reload_adapter()
        log("SETUP 完成首次配置 mode=%s base_url=%s config=%s"
            % (result["mode"], result["base_url"], result["config_path"]))

        # 顺手验一次能不能登录路由器；失败不影响保存，只回显提示
        checked, detail = "skipped", ""
        if result["mode"] == "miwifi":
            try:
                ADAPTER.get_stok(force_refresh=True)
                checked = "ok"
            except Exception as exc:
                checked, detail = "fail", str(exc)
                log("SETUP 路由器登录验证失败：%s" % exc)

        return self._json({
            "ok": True,
            "admin_password": result["admin_password"],
            "same_as_router": result["same_as_router"],
            "config_path": result["config_path"],
            "router_check": checked,
            "router_detail": detail,
        })

    def _payload(self):
        size = int(self.headers.get("Content-Length", "0") or 0)
        try:
            data = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _start(self):
        ip = client_ip(self)
        left = locked_seconds(ip)
        if left:
            return self._json({"ok": False, "reason": "locked", "seconds": left}, 429)
        payload = self._payload()
        if payload is None:
            return self._json({"ok": False, "reason": "bad json"}, 400)
        target = str(payload.get("target") or "").strip().lower()
        try:
            if target:
                # 代答：为指定的受控设备做题（家长用自己手机替孩子放行）
                mac = target
                bank = STORE.bank_for_mac(mac)
                rec = load_grants().get(mac) or {}
                if not bank:
                    return self._json({"ok": False, "reason": "not_managed",
                                       "detail": "这台设备不在受控清单里"}, 403)
            else:
                mac, bank, rec = device_context(ip)
        except Exception as exc:
            log("MAC-FAIL ip=%s err=%s" % (ip, exc))
            return self._json({"ok": False, "reason": "router_error",
                               "detail": str(exc)}, 502)
        if not bank:
            return self._json({"ok": False, "reason": "not_managed",
                               "detail": "这台设备不在受控清单里"}, 403)
        try:
            tier_index = int(payload.get("tier", 0))
        except (TypeError, ValueError):
            tier_index = 0
        if not 0 <= tier_index < len(bank["tiers"]):
            return self._json({"ok": False, "reason": "bad_tier",
                               "detail": "档位不存在，请刷新页面"}, 400)
        cap = int(STORE.snapshot()["settings"].get("daily_cap_minutes", 0) or 0)
        tier = bank["tiers"][tier_index]
        if cap and tier["minutes"] > 0:
            room = cap - granted_today(rec)
            if room <= 0:
                return self._json({
                    "ok": False, "reason": "daily_cap",
                    "detail": "今天的 %s 分钟额度已经用完，明天再来" % cap,
                }, 409)
        sess = start_round(ip, mac, bank, tier_index)
        data = round_payload(ip, sess)
        data["note"] = STORE.snapshot()["settings"]["note"]
        data["mode"] = STORE.snapshot()["router"]["mode"]
        return self._json(data)

    def _answer(self):
        ip = client_ip(self)
        left = locked_seconds(ip)
        if left:
            return self._json({"ok": False, "reason": "locked", "seconds": left}, 429)
        payload = self._payload()
        if payload is None:
            return self._json({"ok": False, "reason": "bad json"}, 400)

        sess = get_round(ip)
        if not sess or sess["idx"] >= len(sess["questions"]):
            return self._json({"ok": False, "reason": "stale",
                               "detail": "答题进度已重置，请重新选择题目数"}, 409)
        q = sess["questions"][sess["idx"]]
        if str(payload.get("id") or "") != q["id"]:
            return self._json({"ok": False, "reason": "stale",
                               "detail": "题目已变更，请按最新题目作答",
                               "question": public_question(q),
                               "index": sess["idx"],
                               "tier": sess["tier"],
                               "total": len(sess["questions"])}, 409)

        choices = payload.get("choices")
        if isinstance(choices, (int, str)):
            choices = [choices]
        if not isinstance(choices, list) or not choices:
            return self._json({"ok": False, "reason": "bad choices",
                               "detail": "请先选择答案"}, 400)

        ok = is_correct(q, choices)
        with _mutex:
            sess["results"].append(ok)
            sess["idx"] += 1
            sess["ts"] = time.time()
            idx = sess["idx"]
            total = len(sess["questions"])
            results = list(sess["results"])
            all_ok = all(results)

        feedback = {
            "ok": True,
            "correct": ok,
            "answer": q["answer"],
            "explain": q.get("explain", ""),
            "index": idx,
            "total": total,
            "results": results,
            "tier": sess["tier"],
            "bank": {"id": sess["bank_id"], "name": sess["bank_name"]},
        }

        if idx < total:
            feedback["done"] = False
            feedback["question"] = public_question(sess["questions"][idx])
            return self._json(feedback)

        # 一轮结束
        feedback["done"] = True
        if all_ok:
            try:
                rec = grant(ip, sess["mac"], sess["bank_id"],
                            sess["bank_name"], sess["minutes"])
            except DailyCap as exc:
                log("DAILY-CAP ip=%s %s" % (ip, exc))
                return self._json({"ok": False, "reason": "daily_cap",
                                   "detail": str(exc)}, 409)
            except NotManaged as exc:
                log("NOT-MANAGED ip=%s %s" % (ip, exc))
                return self._json({"ok": False, "reason": "not_managed",
                                   "detail": str(exc)}, 403)
            except Exception as exc:
                log("ALLOW-FAIL ip=%s err=%s" % (ip, exc))
                return self._json({"ok": False, "reason": "router_error",
                                   "detail": str(exc)}, 502)
            with _mutex:
                _wrong.pop(ip, None)
                _sessions.pop(ip, None)
            feedback.update({
                "all_correct": True,
                "mac": rec["mac"],
                "minutes": rec["minutes"],
                "expire_at": rec["expire_at"],
                "now": int(time.time()),
                "remaining_minutes": rec.get("minutes_left"),
                "today_minutes": rec.get("today_minutes", 0),
                "message": ("已放行，一直有效" if rec["minutes"] == 0
                            else "已放行，本次 +%s 分钟，累计可用 %s 分钟"
                                 % (rec["minutes"], rec.get("minutes_left"))),
            })
            return self._json(feedback)

        count, remain = note_wrong(ip)
        log("WRONG-ROUND ip=%s count=%s results=%s" % (ip, count, results))
        with _mutex:
            _sessions.pop(ip, None)
        feedback["all_correct"] = False
        feedback["wrong_count"] = count
        feedback["remain"] = remain
        feedback["locked_for"] = locked_seconds(ip)
        return self._json(feedback)


def restore_all():
    grants = load_grants()
    if not grants:
        print("没有已记录的设备，无需恢复。")
        return
    print("将放行 %s 台已记录设备（模式：%s）" % (len(grants), ADAPTER.describe()))
    for mac, rec in grants.items():
        try:
            out = ADAPTER.set_wan(mac, True)
            print("OK   %-20s %-16s %s" % (mac, rec.get("ip", ""), str(out)[:100]))
        except Exception as exc:
            print("FAIL %-20s %-16s %s" % (mac, rec.get("ip", ""), exc))


def main():
    if "--restore-all" in sys.argv:
        restore_all()
        return
    # 监听端口/地址的取值集中在 Store 里处理（支持 WIFIGATE_PORT / WIFIGATE_BIND
    # 环境变量覆盖，见 store.py），这里只管用。
    cfg = STORE.snapshot()["server"]
    threading.Thread(target=reblock_worker, daemon=True).start()
    server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), Handler)
    server.daemon_threads = True

    def on_term(signum, frame):
        log("收到信号 %s，优雅退出" % signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    summary = STORE.summary()
    log("服务启动 port=%s 适配器=%s 题库=%s 套/%s 道"
        % (cfg["port"], ADAPTER.describe(), summary["banks"], summary["questions"]))
    for b in summary["by_bank"]:
        print("  · %-8s %3s 道题 %s 台设备 %s 档"
              % (b["name"], b["questions"], b["devices"], b["tiers"]))
    print("答题页： http://<NAS内网IP>:%s/" % cfg["port"])
    print("管理后台： http://<NAS内网IP>:%s/admin" % cfg["port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
