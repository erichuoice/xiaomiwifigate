#!/usr/bin/env python3
"""wifi-gate：答对题目才放行设备上网（多题库 + 时间档位）。

启动：         python3 server.py
应急一键放行： python3 server.py --restore-all
管理后台：     http://<NAS内网IP>:8080/admin

答题流程（参考「题了个题」的逐题模式）：
  GET  /api/quiz    返回本机所属题库、可选档位（答 N 题换 M 分钟）、剩余时间、放行规则
  POST /api/start   选一档开始作答，返回本轮第一题
  POST /api/answer  逐题判卷 → 返回对错、正确答案、解析，再给下一题
  判卷规则（3.11.0 起在后台可配）：
    rate（默认）：答错当场在本轮追加题目，一轮结束正确率 ≥ 阈值（默认 80%）即放行
    strict：一轮全部答对才放行；有错则整轮重来，并计入一次错误
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
import auto_bank
import learning
import router_adapter
import schedule
import store
import updater

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
UPDATE_CHECK_INTERVAL = 6 * 3600   # 自动检查更新：每 6 小时
UPDATE_FIRST_DELAY = 120           # 启动后先让业务跑稳，再去看有没有新版本
AUTO_BANK_INTERVAL = 30            # 定时自动出题：每 30 秒看一眼是否到点
AUTO_BANK_FIRST_DELAY = 20         # 启动后先让业务跑稳

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


def schedule_public(st):
    """时段状态里给前端看的字段（不含服务端内部时间细节）。"""
    return {
        "limited": st["limited"], "allowed": st["allowed"],
        "unlimited": st["unlimited"], "hint": schedule.hint(st),
        "next_label": st["next_label"], "summary": st["summary"],
        "today_text": st["today_text"], "ends_hhmm": st["ends_hhmm"],
        "change_at": st["change_at"],
    }


def schedule_of(mac, now=None):
    """取一台设备的时段状态；没配规则时返回「不限制」。"""
    return schedule.window_state(STORE.schedule_for_mac(mac), now)


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
        "star": q.get("star", 2), "topic": learning.topic_of(q),
    }


def start_round(ip, mac, bank, tier_index):
    tier = bank["tiers"][tier_index]
    pool = bank["questions"]
    n = max(1, min(int(tier["questions"]), len(pool)))
    # 智能组卷：到期错题 / 类似题 + 弱项加权 + 三种题型混合
    try:
        questions = learning.pick_questions(pool, mac, n)
        if not questions:
            questions = random.sample(pool, n)
    except Exception as exc:
        log("PICK-FAIL mac=%s err=%s（回退随机组卷）" % (mac, exc))
        questions = random.sample(pool, n)
    try:
        focus = learning.weak_topics_for(mac, 3)
    except Exception:
        focus = []
    sess = {
        "bank_id": bank["id"], "bank_name": bank["name"], "mac": mac,
        "tier": tier_index, "minutes": int(tier["minutes"]),
        "questions": questions, "focus": focus,
        # 本轮采用题库自己的放行规则；开答后后台改了规则也不会打断这一轮
        "pass_rule": pass_rule(bank),
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
        "focus": sess.get("focus") or [],
        "locked_for": locked_seconds(ip),
    }


def is_correct(q, choices):
    expected = q["answer"] if isinstance(q["answer"], list) else [q["answer"]]
    try:
        got = sorted({int(c) for c in choices})
    except (TypeError, ValueError):
        return False
    return got == sorted(expected)


def pass_rule(bank=None):
    """放行规则（3.12.0：存在题库上，按设备所属题库判卷）。

    老数据里的题库没有这几个字段时，回落到全局设置 → 内置默认值。
    """
    s = STORE.snapshot()["settings"]
    b = bank if isinstance(bank, dict) else {}
    mode = b.get("pass_mode") or s.get("pass_mode") or "rate"
    rate = b.get("pass_rate_percent")
    if rate is None:
        rate = s.get("pass_rate_percent", 80)
    add = b.get("wrong_add_questions")
    if add is None:
        add = s.get("wrong_add_questions", 2)
    try:
        rate_v, add_v = int(rate), int(add)
    except (TypeError, ValueError):
        rate_v, add_v = 80, 2
    return {"mode": "strict" if mode == "strict" else "rate",
            "rate": rate_v, "add": max(0, add_v)}


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
    """放行并重新计时：从现在起算本次档位时长，不在原有到期时间上叠加。

    设备设了答题时段时，本次放行最多到「当前时段结束」，不会跨进禁止时段；
    若答题过程中刚好跨过时段边界，则拒绝放行（不碰路由器）。
    """
    cap = int(STORE.snapshot()["settings"].get("daily_cap_minutes", 0) or 0)
    now = time.time()
    day = today()
    st = schedule_of(mac, now)
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        today_before = granted_today(rec)
        granted = int(minutes)
        if cap > 0 and granted > 0:
            granted = max(0, min(granted, cap - today_before))
            if granted == 0:
                raise DailyCap("今天的 %s 分钟额度已用完，明天再来" % cap)
        if granted == 0 and int(minutes) == 0:
            expire = 0                      # 档位设为 0 分钟 = 永久放行
        else:
            expire = int(now + granted * 60)   # 不叠加：按本次档位重新起算
        ends_hhmm = None
        if st["limited"]:
            if not st["allowed"]:
                raise schedule.OutsideSchedule(schedule.hint(st))
            if expire == 0 or expire > st["ends_at"]:
                # 只放行到本时段结束；至少留 1 秒，避免被当成「永久放行」
                expire = max(int(st["ends_at"]), int(now) + 1)
                ends_hhmm = st["ends_hhmm"]
                granted = max(0, int(math.ceil((expire - now) / 60.0)))
        ADAPTER.set_wan(mac, True)
        rec.update({
            "ip": ip, "mac": mac, "at": now, "expire_at": expire,
            "minutes": granted, "bank": bank_id, "bank_name": bank_name,
            "day": day, "today_minutes": today_before + granted,
            "reblocked": False,
            # 答题放行不是家长手动破例，要受时段规则自动回收
            "manual": False,
        })
        grants[mac] = rec
        save_grants(grants)
    log("ALLOW ip=%s mac=%s bank=%s +%smin expire=%s%s"
        % (ip, mac, bank_name, granted, expire or "永久",
           "（时段至 %s）" % ends_hhmm if ends_hhmm else ""))
    rec["minutes_left"] = minutes_left(rec)
    if ends_hhmm:
        rec["ends_hhmm"] = ends_hhmm
    return rec


def manual_block(mac, reason="手动断网"):
    """家长手动断网：调路由器断开，并把放行记录标记为已回收。

    到期时间写成现在，答题页会立刻显示「已断网」，答一轮题即可恢复。
    """
    ADAPTER.set_wan(mac, False)
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        now = time.time()
        rec.update({"mac": mac, "expire_at": int(now), "reblocked": True,
                    "reblocked_at": now, "manual": True})
        grants[mac] = rec
        save_grants(grants)
    log("MANUAL-BLOCK mac=%s（%s）" % (mac, reason))
    return rec


def manual_unblock(mac, minutes):
    """家长手动放行：minutes=0 表示永久放行（不会被自动断网回收）。

    手动放行是家长意志，不走每日上限；也不计入 today_minutes。
    """
    minutes = max(0, int(minutes))
    ADAPTER.set_wan(mac, True)
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        now = time.time()
        bank = STORE.bank_for_mac(mac) or {}
        if rec.get("day") != today():
            rec["today_minutes"] = 0
        expire = 0 if minutes == 0 else int(now + minutes * 60)
        rec.update({
            "mac": mac, "at": now, "expire_at": expire, "minutes": minutes,
            "bank": bank.get("id", ""), "bank_name": bank.get("name", ""),
            "day": today(), "reblocked": False, "manual": True,
        })
        grants[mac] = rec
        save_grants(grants)
    log("MANUAL-ALLOW mac=%s（手动放行 %s）"
        % (mac, "永久" if minutes == 0 else "+%smin" % minutes))
    rec["minutes_left"] = minutes_left(rec)
    return rec


_sched_blocked = {}   # mac → 是否已因「禁止时段」断过网，避免每轮重复调路由器


def reblock_worker():
    """到点回收 + 时段兜底：配额用完、或进入禁止答题时段，把设备断网。"""
    while True:
        time.sleep(REBLOCK_INTERVAL)
        try:
            with _grants_lock:
                grants = load_grants()
                now = time.time()
                changed = False

                # ---- 答题时段：进入禁止时段时，设备保持断网 ----
                for mac, sched in STORE.schedules().items():
                    st = schedule.window_state(sched, now)
                    if not st["limited"]:
                        _sched_blocked.pop(mac, None)
                        continue
                    if st["allowed"]:
                        _sched_blocked[mac] = False
                        continue
                    rec = grants.get(mac)
                    # 只回收「本系统通过答题发放、且仍在生效」的放行；
                    # 家长手动放行是临时破例，优先保留；没有放行记录的设备
                    # 本来就没被本系统开过网，不去动路由器，避免误伤。
                    exp = (rec or {}).get("expire_at")
                    active = bool(rec) and not rec.get("reblocked") and \
                        (exp == 0 or (exp and exp > now))
                    if not active or (rec.get("manual") and not rec.get("reblocked")):
                        continue
                    if _sched_blocked.get(mac):
                        continue
                    try:
                        ADAPTER.set_wan(mac, False)
                        _sched_blocked[mac] = True
                        rec["reblocked"] = True
                        rec["reblocked_at"] = now
                        rec["reblocked_reason"] = "schedule"
                        rec["expire_at"] = int(now)
                        changed = True
                        log("SCHEDULE-BLOCK mac=%s（%s；下次 %s）"
                            % (mac, st["today_text"], st["next_label"] or "—"))
                    except Exception as exc:
                        log("SCHEDULE-BLOCK-FAIL mac=%s err=%s" % (mac, exc))

                # ---- 原有：配额到期回收 ----
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


def update_worker():
    """定时看远端版本；开了「自动安装」就直接换代码并重启。

    检查失败只记日志、不重试轰炸：下一轮（6 小时后）或用户手动点一次就好。
    """
    time.sleep(UPDATE_FIRST_DELAY)
    while True:
        try:
            settings = STORE.snapshot()["settings"]
            if settings.get("update_auto_check", True):
                rec = updater.check()
                if rec.get("error"):
                    log("UPDATE-CHECK-FAIL %s" % rec["error"])
                elif not rec.get("newer"):
                    log("UPDATE-CHECK 已是最新版本 %s" % rec.get("current"))
                else:
                    log("UPDATE-CHECK 发现新版本 %s → %s"
                        % (rec.get("current"), rec.get("latest")))
                    if settings.get("update_auto_apply"):
                        done = updater.install(expect=rec["latest"])
                        log("UPDATE-APPLY %s → %s（%s 个文件，备份 %s），准备重启"
                            % (done["from"], done["to"], done["files"],
                               done["backup"]))
                        updater.schedule_restart()
                        return
        except Exception as exc:  # 守护线程不能死
            log("UPDATE-WORKER err=%s" % exc)
        time.sleep(UPDATE_CHECK_INTERVAL)


def auto_bank_worker():
    """无人值守补题：每个题库一条规则，哪库「没做过」的题少于它自己的阈值
    就自动给哪库生成一批新题并入库。

    配置在 state/auto_bank.json（3.10.0 起每库一条规则）；每 30 秒检查一次，
    各库冷却独立计时，生成失败也进入冷却，单次执行与后台「立即试跑」
    共用锁，不会并发写题库。
    """
    time.sleep(AUTO_BANK_FIRST_DELAY)
    while True:
        try:
            for rule in auto_bank.load_all(STORE):
                if not auto_bank.due(STORE, rule):
                    continue
                rec = auto_bank.run_once(STORE, rule, "auto")
                if rec.get("ok"):
                    log("AUTO-BANK %s 补充新题：生成 %s 道，入库 %s 道（重复 %s），"
                        "淘汰旧题 %s 道，未做过 %s/%s 道，用时 %ss"
                        % (rec["bank_name"], rec["generated"], rec["added"],
                           rec["skipped"], rec["pruned"], rec["fresh"],
                           rec["total"], rec["latency"]))
                else:
                    log("AUTO-BANK-FAIL %s" % rec.get("detail"))
        except Exception as exc:  # 守护线程不能死
            log("AUTO-BANK-WORKER err=%s" % exc)
        time.sleep(AUTO_BANK_INTERVAL)


# ---- HTTP ----
class Handler(BaseHTTPRequestHandler):
    server_version = "wifi-gate/" + updater.version()

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
        # 手动断网/放行（后台设备页按钮与 gatectl 的服务端入口）
        if path == "/api/admin/device/wan":
            if not STORE.check_session(admin_api.cookie_token(self.headers)):
                return self._json({"ok": False, "detail": "请先登录后台"}, 401)
            if method == "GET":
                grants = load_grants()
                owner = {mac: b for b in STORE.banks()
                         for mac in b["devices"]}
                try:
                    names = ADAPTER.names_by_mac()
                except Exception:
                    names = {}
                # 在线设备（路由器设备列表）；拉不到时退化为只列受控设备
                try:
                    rows = router_adapter.normalize_devices(
                        json.loads(ADAPTER.device_list_raw()))
                except Exception as exc:
                    log("设备页拉取路由器设备列表失败：%s" % exc)
                    rows = []
                out = []
                seen = set()
                for r in rows:
                    mac = r["mac"].lower()
                    seen.add(mac)
                    b = owner.get(mac)
                    rec = grants.get(mac) or {}
                    left = minutes_left(rec)
                    out.append({
                        "mac": mac,
                        "name": names.get(mac) or r.get("name") or "",
                        "bank_id": b["id"] if b else "",
                        "bank_name": b["name"] if b else "（不受控）",
                        "remaining_minutes": left,
                        "expire_at": rec.get("expire_at"),
                        "reblocked": bool(rec.get("reblocked")),
                        "today_minutes": (rec.get("today_minutes", 0)
                                          if rec.get("day") == today() else 0),
                        "state": ("永久放行" if left is None
                                  else "上网中" if left > 0
                                  else "已断网" if r.get("wan") in (0, "0")
                                  else "答题外放行"),
                    })
                # 已分配但当前不在线的设备也列出，避免「看不见的条目」
                for mac, b in owner.items():
                    if mac.lower() in seen:
                        continue
                    rec = grants.get(mac) or {}
                    left = minutes_left(rec)
                    out.append({
                        "mac": mac.lower(),
                        "name": names.get(mac) or "（当前不在线）",
                        "bank_id": b["id"], "bank_name": b["name"],
                        "remaining_minutes": left,
                        "expire_at": rec.get("expire_at"),
                        "reblocked": bool(rec.get("reblocked")),
                        "today_minutes": (rec.get("today_minutes", 0)
                                          if rec.get("day") == today() else 0),
                        "state": ("永久放行" if left is None
                                  else "上网中" if left > 0 else "已断网"),
                    })
                return self._json({
                    "ok": True, "devices": out,
                    # 前端按「服务端时间 + 本地流逝」算倒计时，避免两端时钟差
                    "server_now": time.time(),
                })
            if method != "POST":
                return self._json({"ok": False, "detail": "请用 POST"}, 405)
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
            mac = str(data.get("mac") or "").strip().lower()
            want = data.get("wan")
            try:
                minutes = int(data.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if not mac:
                # 明确的入参错误：不要落到「设备不存在」的 404 上误导排查。
                return self._json({
                    "ok": False,
                    "detail": "请求里没有 MAC：后台页面可能是旧版本，"
                              "请强制刷新页面（Ctrl/Cmd+Shift+R）后重试",
                }, 400)
            # 手动控制不要求设备已分配题库：家长可对设备列表里
            # 的任何一台直接断网/放行。但 MAC 必须真实存在，防手滑。
            known = {m.lower() for b in STORE.banks() for m in b["devices"]}

            def fetch_known(force=False):
                if force:
                    ADAPTER.get_stok(force_refresh=True)
                return {r["mac"].lower() for r in router_adapter.normalize_devices(
                    json.loads(ADAPTER.device_list_raw()))}

            router_ok = True
            try:
                known |= fetch_known()
            except Exception as exc:
                router_ok = False
                log("手动控制时拉取路由器设备列表失败：%s" % exc)
            if mac and mac not in known and router_ok:
                # 第一查没找到：强制重新登录再查一次，
                # 覆盖「列表短暂不一致 / stok 半失效」的情况。
                try:
                    known |= fetch_known(force=True)
                except Exception as exc:
                    log("手动控制第二次拉取设备列表失败：%s" % exc)
            if mac not in known:
                if not router_ok:
                    # 区分「路由器没连上」和「真没这台设备」，
                    # 避免家长被误导去查 MAC。
                    return self._json({
                        "ok": False,
                        "detail": "路由器设备列表暂时拉取失败（已自动重登），"
                                  "请刷新页面再试一次",
                    }, 502)
                # 记下被拒的 MAC，便于排查「明明列表里有」类反馈。
                log("手动控制拒绝：mac=%s 不在设备列表（共 %s 台）" % (mac, len(known)))
                return self._json(
                    {"ok": False,
                     "detail": "路由器设备列表里没有这台设备，"
                               "收到的 MAC 是 %s；请刷新设备页后重试" % mac},
                    404)
            try:
                if want is True:
                    rec = manual_unblock(mac, minutes)
                elif want is False:
                    rec = manual_block(mac)
                else:
                    return self._json(
                        {"ok": False, "detail": "wan 需为 true（放行）或 false（断网）"},
                        400)
            except Exception as exc:
                return self._json(
                    {"ok": False, "detail": "路由器接口失败：%s" % exc}, 502)
            return self._json({"ok": True, "record": rec})
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
                "version": updater.version(),
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
            "pass_rule": pass_rule(),
            "server_now": time.time(),
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
        now = time.time()
        scheds = STORE.schedules()
        targets = []
        for t in STORE.targets():
            trec = grants.get(t["mac"]) or {}
            targets.append({
                "mac": t["mac"],
                "name": names.get(t["mac"], ""),
                "bank": {"id": t["bank_id"], "name": t["bank_name"]},
                "tiers": t["tiers"],
                "pass_rule": t.get("pass_rule") or pass_rule(),
                "remaining_minutes": minutes_left(trec),
                "today_minutes": granted_today(trec),
                "is_self": t["mac"] == mac,
                "schedule": schedule_public(
                    schedule.window_state(scheds.get(t["mac"]), now)),
            })
        base["targets"] = targets
        base["schedule"] = None

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
            "pass_rule": pass_rule(bank),
            "remaining_minutes": minutes_left(rec),
            "today_minutes": used,
            "daily_cap_minutes": cap,
            "today_left": (max(0, cap - used) if cap else None),
            "schedule": schedule_public(
                schedule.window_state(scheds.get(mac), now)),
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
        # 时段外的设备不能开始答题（代答同样受这台设备的规则约束）
        st = schedule_of(mac)
        if st["limited"] and not st["allowed"]:
            return self._json({
                "ok": False, "reason": "outside_schedule",
                "detail": schedule.hint(st),
                "schedule": schedule_public(st),
            }, 403)
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

        # ---- 正确率模式：答错当场在本轮末尾追加题目 ----
        # 规则来自本轮的题库（3.12.0：每套题库各配各的），开答时已快照进会话。
        rule = sess.get("pass_rule") or pass_rule(STORE.bank(sess["bank_id"]))
        rule_mode = rule.get("mode") or "rate"
        rate_mode = rule_mode != "strict"
        added = 0
        if not ok and rate_mode:
            add_n = int(rule.get("add") or 0)
            if add_n > 0:
                try:
                    used = {x["id"] for x in sess["questions"]}
                    bank = STORE.bank(sess["bank_id"]) or {}
                    pool = [x for x in (bank.get("questions") or [])
                            if x["id"] not in used]
                    if pool:
                        want = min(add_n, len(pool))
                        try:
                            extra = learning.pick_questions(
                                pool, sess["mac"], want)
                        except Exception:
                            extra = []
                        if not extra:
                            extra = random.sample(pool, want)
                        with _mutex:
                            sess["questions"].extend(extra)
                            sess["ts"] = time.time()
                            total = len(sess["questions"])
                        added = len(extra)
                except Exception as exc:  # 加题失败不影响判卷
                    log("WRONG-ADD-FAIL ip=%s err=%s" % (ip, exc))

        # 一轮结束的判卷：strict = 全对；rate = 正确率达标
        correct_n = results.count(True)
        accuracy = int(round(correct_n * 100.0 / total)) if total else 0
        if idx >= total:
            if rate_mode:
                rate = int(rule.get("rate") or 80)
                passed = correct_n * 100 >= rate * total
            else:
                passed = all(results)
        else:
            passed = False

        # 学情：逐题记入错题本 / 知识点掌握度，一轮结束记轮次。
        # 记录失败绝不能影响判卷与放行。
        try:
            learning.record_answer(sess["mac"], q, ok)
            if idx >= total:
                learning.record_round(sess["mac"], passed)
        except Exception as exc:
            log("LEARNING-FAIL mac=%s err=%s" % (sess["mac"], exc))

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
            "added_questions": added,
            "pass_mode": rule_mode,
        }

        if idx < total:
            feedback["done"] = False
            feedback["question"] = public_question(sess["questions"][idx])
            return self._json(feedback)

        # 一轮结束
        feedback["done"] = True
        if rate_mode:
            feedback["pass_rate_percent"] = int(rule.get("rate") or 80)
            feedback["accuracy_percent"] = accuracy
        if passed:
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
            except schedule.OutsideSchedule as exc:
                # 答题过程中跨过了允许时段的边界：不放行，保持断网
                log("OUTSIDE-SCHEDULE ip=%s mac=%s %s"
                    % (ip, sess["mac"], exc))
                with _mutex:
                    _sessions.pop(ip, None)
                return self._json({"ok": False, "reason": "outside_schedule",
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
                "ends_hhmm": rec.get("ends_hhmm"),
                "message": (
                    "已放行到 %s（本时段结束），可用 %s 分钟"
                    % (rec["ends_hhmm"], rec.get("minutes_left"))
                    if rec.get("ends_hhmm") else
                    ("已放行，一直有效" if rec["minutes"] == 0
                     else "已放行，本次 +%s 分钟，可用 %s 分钟"
                          % (rec["minutes"], rec.get("minutes_left")))),
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
    threading.Thread(target=update_worker, daemon=True).start()
    threading.Thread(target=auto_bank_worker, daemon=True).start()
    server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), Handler)
    server.daemon_threads = True

    def on_term(signum, frame):
        log("收到信号 %s，优雅退出" % signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    summary = STORE.summary()
    log("服务启动 port=%s 版本=%s 适配器=%s 题库=%s 套/%s 道"
        % (cfg["port"], updater.version(), ADAPTER.describe(),
           summary["banks"], summary["questions"]))
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
