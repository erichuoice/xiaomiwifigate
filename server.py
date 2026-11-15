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
import feedback
import mistakes
import learning
import router_adapter
import savings
import schedule
import store
import updater

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
STATE = os.path.join(BASE, "state")
os.makedirs(STATE, exist_ok=True)

LOG_PATH = os.path.join(STATE, "access.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
SESSION_TTL = 1800
MAC_CACHE_TTL = 120
REBLOCK_INTERVAL = 20
UPDATE_CHECK_INTERVAL = 6 * 3600   # 自动检查更新：每 6 小时
UPDATE_FIRST_DELAY = 120           # 启动后先让业务跑稳，再去看有没有新版本
AUTO_BANK_INTERVAL = 30            # 定时自动出题：每 30 秒看一眼是否到点
AUTO_BANK_FIRST_DELAY = 20         # 启动后先让业务跑稳

_log_lock = threading.Lock()
_grants_lock = threading.Lock()
_release_lock = threading.Lock()   # 3.50.1：消费（查余额→放行→扣款）必须串行，防并发少扣多放
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


# ---- 放行记录（3.50.1 起在 grants.py，后台首页也用同一份）----

from grants import (GRANTS_PATH, today, load_grants, save_grants,  # noqa: E402
                    minutes_left, granted_today, earned_today_for)


def earn_mode():
    """当前答题所得的去向：savings（默认，先进储蓄）/ direct（直接放行）。"""
    mode = str(STORE.snapshot()["settings"].get("savings_mode", "savings"))
    return "direct" if mode == "direct" else "savings"


def schedule_public(st):
    """时段状态里给前端看的字段（不含服务端内部时间细节）。"""
    return {
        "limited": st["limited"], "allowed": st["allowed"],
        "unlimited": st["unlimited"], "hint": schedule.hint(st),
        # 3.17.0：免答题的自由上网时段
        "free": bool(st.get("free")), "free_hhmm": st.get("free_hhmm"),
        "next_label": st["next_label"], "summary": st["summary"],
        "today_text": st["today_text"], "ends_hhmm": st["ends_hhmm"],
        # 3.50.0：答题页要用 ends_at 算「本时段还能消费多少分钟」，再决定要不要提醒
        "ends_at": st.get("ends_at"), "next_start": st.get("next_start"),
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
        "chapter": q.get("chapter", ""), "source": q.get("source", ""),
    }


def start_round(ip, mac, bank, tier_index):
    tier = bank["tiers"][tier_index]
    pool = bank["questions"]
    n = max(1, min(int(tier["questions"]), len(pool)))
    # 智能组卷（3.13.0 四层优先级）：错题 40% → 新题 40% → 毕业错题 10% → 已会题 10%
    try:
        questions = learning.pick_questions(pool, bank["id"], n)
        if not questions:
            questions = random.sample(pool, n)
    except Exception as exc:
        log("PICK-FAIL mac=%s err=%s（回退随机组卷）" % (mac, exc))
        questions = random.sample(pool, n)
    try:
        focus = learning.weak_topics_for(bank["id"], 3)
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


def limit_rule(bank=None):
    """限制方式（3.14.0：存在题库上）：block = 到点断网，throttle = 到点限速。

    throttle 时用 throttle_kbps（单位 KB/s，2 Mbps ≈ 250 KB/s）限这台设备，
    做题放行后解除限速，不会真的切断它的外网。
    """
    b = bank if isinstance(bank, dict) else {}
    mode = str(b.get("limit_mode") or "block").lower()
    try:
        kbps = int(b.get("throttle_kbps") or 0)
    except (TypeError, ValueError):
        kbps = 0
    if kbps <= 0:
        kbps = 20
    return {"mode": "throttle" if mode == "throttle" else "block",
            "kbps": kbps}


def restrict_mac(mac, bank=None, reason="到期"):
    """按题库设定的限制方式「限制」一台设备：断网或限速。

    限速模式下不切断外网，只把上下行降到题库配置的 KB/s，
    所以孩子仍然能上网（很慢），答对题就恢复全速。
    """
    rule = limit_rule(bank)
    if rule["mode"] == "throttle":
        ADAPTER.set_limit(mac, rule["kbps"], rule["kbps"])
        ADAPTER.set_wan(mac, True)      # 限速模式不切断，确保只是变慢
        log("THROTTLE mac=%s %sKB/s（%s）" % (mac, rule["kbps"], reason))
        return "throttle"
    ADAPTER.set_wan(mac, False)
    return "block"


def release_mac(mac, bank=None, reason="放行"):
    """解除限制：断网模式恢复外网；限速模式把限速清零（恢复全速）。

    断网模式也顺手把残留限速清掉——题库可能刚从「限速」改回「断网」，
    不清的话设备会既恢复外网又还慢着，家长看不出问题在哪。
    """
    rule = limit_rule(bank)
    if rule["mode"] == "throttle":
        ADAPTER.set_limit(mac, 0, 0)
        ADAPTER.set_wan(mac, True)
        log("THROTTLE-OFF mac=%s（%s）" % (mac, reason))
        return "throttle"
    try:
        ADAPTER.set_limit(mac, 0, 0)   # 清残留限速；失败不阻断放行本身
    except Exception as exc:
        log("清理残留限速失败（忽略）mac=%s err=%s" % (mac, exc))
    ADAPTER.set_wan(mac, True)
    return "block"


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

def grant(ip, mac, bank_id, bank_name, minutes, count_cap=True):
    """放行并重新计时：从现在起算本次档位时长，不在原有到期时间上叠加。

    设备设了消费时段时，本次放行最多到「当前时段结束」，不会跨进禁止时段；
    若答题过程中刚好跨过时段边界，则拒绝放行（不碰路由器）。

    count_cap=False 用于「消费储蓄」：这些分钟在存入时已经计入每日上限，
    释放时不再重复扣额度。
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
        if count_cap and cap > 0 and granted > 0:
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
        release_mac(mac, STORE.bank(bank_id), "答题放行")
        rec.update({
            "ip": ip, "mac": mac, "at": now, "expire_at": expire,
            "minutes": granted, "bank": bank_id, "bank_name": bank_name,
            "day": day, "today_minutes": (today_before + granted) if count_cap else today_before,
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
    """家长手动限制：断网模式下断开；限速模式下把设备降到题库设定的速度。

    到期时间写成现在，答题页会立刻显示「已限制」，答一轮题即可恢复。
    """
    mode = restrict_mac(mac, STORE.bank_for_mac(mac), reason)
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        now = time.time()
        rec.update({"mac": mac, "expire_at": int(now), "reblocked": True,
                    "reblocked_at": now, "reblocked_reason": mode,
                    "manual": True})
        grants[mac] = rec
        save_grants(grants)
    log("MANUAL-%s mac=%s（%s）"
        % ("THROTTLE" if mode == "throttle" else "BLOCK", mac, reason))
    return rec


def manual_unblock(mac, minutes):
    """家长手动放行：minutes=0 表示永久放行（不会被自动断网回收）。

    手动放行是家长意志，不走每日上限；也不计入 today_minutes。
    """
    minutes = max(0, int(minutes))
    bank = STORE.bank_for_mac(mac) or {}
    release_mac(mac, bank, "手动放行")
    with _grants_lock:
        grants = load_grants()
        rec = dict(grants.get(mac) or {})
        now = time.time()
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
    log("MANUAL-ALLOW mac=%s（手动放行 %s%s）"
        % (mac, "永久" if minutes == 0 else "+%smin" % minutes,
           "，限速已解除" if limit_rule(bank)["mode"] == "throttle" else ""))
    rec["minutes_left"] = minutes_left(rec)
    return rec


_sched_blocked = {}   # mac → 是否已因「禁止时段」断过网，避免每轮重复调路由器
_free_opened = {}     # mac → 已经为哪个「自由上网时段」开过网（避免每 30 秒重复调路由器）


def _sync_free_windows(grants, states, now):
    """免答题的「自由上网时段」：到点自动放行，并把放行记录写成到时段结束。

    放行记录 expire_at = 时段结束，所以时段一过，原有的到期回收逻辑
    会把它重新限制（断网或限速），不需要额外代码。
    家长在本时段内手动断网的，尊重家长、不自动放开。
    返回是否有改动。
    """
    changed = False
    for mac in list(_free_opened):
        if mac not in states:          # 规则删掉了：清掉缓存的「开过网」标记
            _free_opened.pop(mac, None)
    for mac, st in states.items():
        if not st.get("limited") or not st.get("free"):
            _free_opened.pop(mac, None)
            continue
        until = int(st["free_until"])
        rec = dict(grants.get(mac) or {})
        # 家长刚在本时段里手动断过网（reblocked 且 manual）：尊重，不自动开
        if (rec.get("manual") and rec.get("reblocked") and
                int(rec.get("reblocked_at") or 0) >= int(st.get("free_start") or 0)):
            _free_opened[mac] = until
            continue
        if (_free_opened.get(mac) or 0) >= until:
            continue
        bank = STORE.bank_for_mac(mac) or {}
        try:
            release_mac(mac, bank, "自由上网时段")
        except Exception as exc:
            log("FREE-ALLOW-FAIL mac=%s err=%s" % (mac, exc))
            continue
        if rec.get("day") != today():
            rec["today_minutes"] = 0
        rec.update({
            "mac": mac, "at": now, "expire_at": until, "minutes": 0,
            "bank": bank.get("id", ""), "bank_name": bank.get("name", ""),
            "day": today(), "reblocked": False, "manual": False, "free": True,
        })
        grants[mac] = rec
        _free_opened[mac] = until
        changed = True
        log("FREE-ALLOW mac=%s（自由上网时段，%s 到 %s）"
            % (mac, bank.get("name", ""), st.get("free_hhmm") or "—"))
    return changed


def reblock_worker():
    """到点回收 + 时段兜底：配额用完、或进入禁止消费时段，把设备断网。"""
    while True:
        time.sleep(REBLOCK_INTERVAL)
        try:
            with _grants_lock:
                grants = load_grants()
                now = time.time()
                changed = False

                # ---- 时段状态一次算好：自由上网时段 / 可消费时段 / 禁止消费时段 ----
                states = {mac: schedule.window_state(sched, now)
                          for mac, sched in STORE.schedules().items()}
                # 免答题时段：到点自动放行（放行记录写到时段结束）
                if _sync_free_windows(grants, states, now):
                    changed = True

                # ---- 可消费时段：进入禁止时段时，设备保持断网（答题不受影响） ----
                for mac, st in states.items():
                    if not st["limited"]:
                        _sched_blocked.pop(mac, None)
                        continue
                    if st["allowed"]:
                        # 自由上网 / 可消费时段：不在这里限制
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
                        mode = restrict_mac(mac, STORE.bank_for_mac(mac),
                                            "禁止消费时段")
                        _sched_blocked[mac] = True
                        rec["reblocked"] = True
                        rec["reblocked_at"] = now
                        rec["reblocked_reason"] = "schedule"
                        rec["expire_at"] = int(now)
                        changed = True
                        log("SCHEDULE-%s mac=%s（%s；下次 %s）"
                            % ("THROTTLE" if mode == "throttle" else "BLOCK",
                               mac, st["today_text"],
                               st["next_label"] or "—"))
                    except Exception as exc:
                        log("SCHEDULE-BLOCK-FAIL mac=%s err=%s" % (mac, exc))

                # ---- 原有：配额到期回收 ----
                for mac, rec in grants.items():
                    if rec.get("reblocked"):
                        continue
                    if (states.get(mac) or {}).get("free"):
                        # 正处在免答题自由上网时段：先不回收，时段结束再收
                        continue
                    expire = int(rec.get("expire_at") or 0)
                    if not expire or now < expire:
                        continue
                    try:
                        mode = restrict_mac(mac, STORE.bank_for_mac(mac),
                                            "配额 %s 分钟用完" % rec.get("minutes"))
                        rec["reblocked"] = True
                        rec["reblocked_at"] = now
                        rec["reblocked_reason"] = mode
                        changed = True
                        log("%s mac=%s（配额 %s 分钟用完）"
                            % ("THROTTLE" if mode == "throttle" else "REBLOCK",
                               mac, rec.get("minutes")))
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
        # 手动限速/解除限速（3.14.0：限速作为「不切断外网」的限制方式）
        if path == "/api/admin/device/limit":
            if not STORE.check_session(admin_api.cookie_token(self.headers)):
                return self._json({"ok": False, "detail": "请先登录后台"}, 401)
            if method != "POST":
                return self._json({"ok": False, "detail": "请用 POST"}, 405)
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
            mac = str(data.get("mac") or "").strip().lower()
            if not mac:
                return self._json({"ok": False, "detail": "请求里没有 MAC"}, 400)
            try:
                kbps = int(float(data.get("kbps") or 0))
            except (TypeError, ValueError):
                return self._json({"ok": False, "detail": "kbps 必须是数字"}, 400)
            if kbps < 0 or kbps > 1000000:
                return self._json(
                    {"ok": False, "detail": "kbps 需在 0-1000000 之间"}, 400)
            try:
                if kbps == 0:
                    # 解除限速：直接清零，不看题库模式（题库模式可能刚被改过，
                    # 按模式走会漏清）；同时把放行记录置为永久，防止回收线程
                    # 又把限速加回去。
                    ADAPTER.set_limit(mac, 0, 0)
                    with _grants_lock:
                        grants = load_grants()
                        rec = dict(grants.get(mac) or {})
                        rec.update({"mac": mac, "expire_at": 0,
                                    "reblocked": False})
                        grants[mac] = rec
                        save_grants(grants)
                    log("MANUAL-THROTTLE-OFF mac=%s（手动解除限速）" % mac)
                else:
                    ADAPTER.set_limit(mac, kbps, kbps)
                    log("MANUAL-THROTTLE mac=%s %sKB/s" % (mac, kbps))
                limits = ADAPTER.limit_of(mac)
            except Exception as exc:
                return self._json(
                    {"ok": False, "detail": "路由器接口失败：%s" % exc}, 502)
            if kbps:
                with _grants_lock:
                    grants = load_grants()
                    rec = dict(grants.get(mac) or {})
                    now = time.time()
                    rec.update({"mac": mac, "expire_at": int(now),
                                "reblocked": True, "reblocked_at": now,
                                "reblocked_reason": "throttle", "manual": True})
                    grants[mac] = rec
                    save_grants(grants)
            return self._json({"ok": True, "mac": mac, "kbps": kbps,
                               "limits": limits})
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
                # 每台设备当前的真实限速值（KB/s），限速模式下列表要显示
                limits = {}
                try:
                    for dev in ADAPTER.qos_info().get("list") or []:
                        q = dev.get("qos") or {}
                        d = int(float(q.get("downmax") or 0))
                        u = int(float(q.get("upmax") or 0))
                        if d or u:
                            limits[str(dev.get("mac", "")).lower()] = {
                                "down": d, "up": u}
                except Exception as exc:
                    log("设备页拉取限速状态失败：%s" % exc)
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
                        "limit_mode": (b.get("limit_mode") or "block") if b else "block",
                        "throttle_kbps": int(b.get("throttle_kbps") or 0) if b else 0,
                        "limit": limits.get(mac, {"down": 0, "up": 0}),
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
                        "limit_mode": b.get("limit_mode") or "block",
                        "throttle_kbps": int(b.get("throttle_kbps") or 0),
                        "limit": limits.get(mac.lower(), {"down": 0, "up": 0}),
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
        # 3.48.0：家长自己的手机（按 MAC 信任）打开后台免密直接进
        ip = client_ip(self)
        headers = self.headers
        auto_cookie = ""
        if (STORE.admin_trusted() and
                not STORE.check_session(admin_api.cookie_token(self.headers))):
            try:
                admin_mac = resolve_mac(ip)
            except Exception as exc:
                admin_mac = ""
                log("ADMIN-TRUST mac 解析失败 ip=%s err=%s" % (ip, exc))
            if admin_mac and STORE.is_trusted_admin(admin_mac):
                token = STORE.issue_session()
                auto_cookie = ("%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
                               % (admin_api.COOKIE, token))
                hdrs = dict(self.headers.items())
                old_cookie = hdrs.get("Cookie", "")
                hdrs["Cookie"] = ((old_cookie + "; ") if old_cookie else "") + \
                    "%s=%s" % (admin_api.COOKIE, token)
                headers = hdrs
                log("ADMIN 免密登录 ip=%s mac=%s" % (ip, admin_mac))
        status, obj, cookie = admin_api.dispatch(
            STORE, ADAPTER, method, self.path, headers, raw, ip)
        if isinstance(obj, admin_api.Raw):
            return self._raw(obj.body, obj.ctype, obj.filename)
        return self._json(obj, status, cookie or auto_cookie)

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
        if path in ("/mistakes", "/mistakes.html"):
            return self._static("mistakes.html", "text/html; charset=utf-8")
        if path == "/api/mistakes":
            try:
                mac, bank, _rec = device_context(client_ip(self))
            except Exception as exc:
                log("MISTAKES-CTX err=%s" % exc)
                mac, bank = "", None
            if bank is None:
                return self._json({"ok": False, "reason": "not_managed",
                                   "detail": "这台设备不在受控清单里，没有错题本。"})
            data = mistakes.analyze(bank)
            data["ok"] = True
            data["mac"] = mac
            return self._json(data)
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
            "limit_rule": limit_rule(),
            "savings_mode": earn_mode(),
            "server_now": time.time(),
        }
        # 3.49.0：时间储蓄按「人」（题库）记账；本机没归属时不显示余额
        base["savings_minutes"] = 0
        base["savings_ledger"] = []
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
                "limit_rule": limit_rule({"limit_mode": t.get("limit_mode"),
                                          "throttle_kbps": t.get("throttle_kbps")}),
                "remaining_minutes": minutes_left(trec),
                "today_minutes": earned_today_for(t["bank_id"], trec),
                "savings_minutes": savings.state_of(t["bank_id"])["minutes"],
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
        used = earned_today_for(bank["id"], rec)
        sav = savings.state_of(bank["id"])
        rule = limit_rule(bank)
        base.update({
            "need": True,
            "pick_target": False,
            "bank": {"id": bank["id"], "name": bank["name"]},
            "tiers": bank["tiers"],
            "pass_rule": pass_rule(bank),
            "limit_rule": rule,
            "remaining_minutes": minutes_left(rec),
            "today_minutes": used,
            "daily_cap_minutes": cap,
            "savings_minutes": sav["minutes"],
            "savings_ledger": sav["ledger"],
            "today_left": (max(0, cap - used) if cap else None),
            "schedule": schedule_public(
                schedule.window_state(scheds.get(mac), now)),
        })
        if rule["mode"] == "throttle":
            # 顺带把机器上真实的限速值带出来，页面/后台不用再单独查
            try:
                base["limited"] = ADAPTER.limit_of(mac)
            except Exception as exc:
                log("LIMIT-QUEUE ip=%s err=%s" % (ip, exc))
        return self._json(base)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/savings/release":
            return self._release_savings()
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
        if path == "/api/feedback":
            return self._feedback()
        if path != "/api/answer":
            return self._json({"error": "not found"}, 404)
        return self._answer()

    def _feedback(self):
        """3.34.0：答题页「这题有问题」反馈（不需要登录，限长度 + 简单节流）。"""
        payload = self._payload()
        if payload is None:
            return self._json({"ok": False, "detail": "请求格式不正确"}, 400)
        note = str(payload.get("note") or "").strip()
        if len(note) > 300:
            return self._json({"ok": False, "detail": "说明太长了（最多 300 字）"}, 400)
        ip = client_ip(self)
        mac, bank = "", None
        try:
            mac, bank, _rec = device_context(ip)
        except Exception as exc:
            log("FEEDBACK-CTX ip=%s err=%s" % (ip, exc))
        if not mac:
            mac = str(payload.get("mac") or "").strip().lower()
        if bank is None and payload.get("bank_id"):
            bank = STORE.bank(str(payload.get("bank_id")))
        # 简单节流：同一台设备 1 分钟内最多 3 条
        now = time.time()
        recent = [x for x in feedback.listing() if x.get("mac") == mac
                  and now - float(x.get("ts") or 0) < 60]
        if mac and len(recent) >= 3:
            return self._json({"ok": False, "detail": "反馈太频繁，请稍后再试"}, 429)
        item = feedback.add(mac=mac, bank_id=(bank or {}).get("id", ""),
                            qid=payload.get("id") or payload.get("qid") or "",
                            text=payload.get("text") or "",
                            note=note)
        log("FEEDBACK mac=%s bank=%s qid=%s note=%s"
            % (mac, (bank or {}).get("id", ""), item.get("qid") if item else "",
               note[:40]))
        return self._json({"ok": True, "id": (item or {}).get("id", "")})

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
        # 3.50.0：时段只管「消费」，savings 模式答题随时都能答（攒时间）。
        # 3.50.1：direct 模式下答题=直接放行，禁止时段还是不让开始，
        # 免得孩子答完一轮才被拒。
        if earn_mode() == "direct":
            st0 = schedule_of(mac)
            if st0["limited"] and not st0["allowed"]:
                return self._json({
                    "ok": False, "reason": "outside_schedule",
                    "detail": schedule.hint(st0),
                    "schedule": schedule_public(st0)}, 403)
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
            room = cap - earned_today_for(bank["id"], rec)
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
                                pool, sess["bank_id"], want)
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
            # 3.18.0：学情按「人」记（题库 id），同一题库下换设备/代答都算同一个人
            learning.record_answer(sess["bank_id"], q, ok)
            if idx >= total:
                learning.record_round(sess["bank_id"], passed)
        except Exception as exc:
            log("LEARNING-FAIL bank=%s err=%s" % (sess["bank_id"], exc))

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
            # 3.49.0：默认「答题所得先进时间储蓄」，孩子在答题页点「消费」才放行；
            # 设置里切成 direct 则保持原来的「答对立刻放行」。
            cfg_s = STORE.snapshot()["settings"]
            if earn_mode() == "savings" and int(sess["minutes"]) > 0:
                cap = int(cfg_s.get("daily_cap_minutes", 0) or 0)
                try:
                    names = ADAPTER.names_by_mac()
                except Exception:
                    names = {}
                try:
                    saved = savings.credit(
                        sess["bank_id"], int(sess["minutes"]), kind="quiz",
                        reason="答对 %s 题" % total,
                        by=names.get(sess["mac"], "") or sess["mac"], cap=cap)
                except Exception as exc:
                    log("SAVE-FAIL ip=%s err=%s" % (ip, exc))
                    return self._json({"ok": False, "reason": "save_error",
                                       "detail": "存入储蓄失败：%s" % exc}, 500)
                if not saved.get("ok"):
                    log("DAILY-CAP ip=%s 储蓄额度已用完" % ip)
                    return self._json({
                        "ok": False, "reason": "daily_cap",
                        "detail": "今天的 %s 分钟额度已经用完，明天再来" % cap,
                    }, 409)
                log("SAVE ip=%s mac=%s bank=%s +%smin 余额=%s"
                    % (ip, sess["mac"], sess["bank_id"],
                       saved["added"], saved["minutes"]))
                with _mutex:
                    _wrong.pop(ip, None)
                    _sessions.pop(ip, None)
                feedback.update({
                    "all_correct": True,
                    "saved": True,
                    "saved_minutes": saved["added"],
                    "savings_minutes": saved["minutes"],
                    "today_minutes": saved["earned_today"],
                    "daily_cap_minutes": cap,
                    "message": (
                        "已存入时间储蓄 %s 分钟（今日额度只剩 %s 分钟），现在有 %s 分钟可以消费"
                        % (saved["added"], cap - saved["earned_today"], saved["minutes"])
                        if saved.get("clamped") else
                        "已存入时间储蓄 %s 分钟，现在有 %s 分钟可以消费"
                        % (saved["added"], saved["minutes"])),
                })
                return self._json(feedback)
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

    def _release_savings(self):
        """把储蓄里的分钟数「消费」成上网时长（孩子在答题页点消费）。

        target 缺省 = 本机；家长手机（本身不受控）可以指定一台受控设备代消费。
        3.50.1：查余额 → 放行 → 扣款 全程加锁，避免并发请求「少扣多放」；
        已受控的孩子只能消费自己名下的设备，不能花掉兄弟姐妹的储蓄。
        """
        ip = client_ip(self)
        payload = self._payload()
        if payload is None:
            return self._json({"ok": False, "reason": "bad json"}, 400)
        try:
            minutes = int(payload.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            return self._json({"ok": False, "reason": "bad_request",
                               "detail": "请选择要释放的分钟数"}, 400)
        target = str(payload.get("target") or "").strip().lower()
        with _release_lock:
            try:
                if target:
                    mac = target
                    bank = STORE.bank_for_mac(mac)
                    _req_mac, req_bank, _req_rec = device_context(ip)
                else:
                    mac, bank, _rec = device_context(ip)
                    req_bank = bank
            except Exception as exc:
                log("MAC-FAIL ip=%s err=%s" % (ip, exc))
                return self._json({"ok": False, "reason": "router_error",
                                   "detail": str(exc)}, 502)
            if not bank:
                return self._json({"ok": False, "reason": "not_managed",
                                   "detail": "这台设备不在受控清单里"}, 403)
            # 3.50.1：孩子只能用自己名下的设备消费；家长手机（无题库）仍可代消费
            if req_bank and req_bank["id"] != bank["id"]:
                return self._json({
                    "ok": False, "reason": "other_student",
                    "detail": "只能消费自己名下设备的时间",
                }, 403)
            bank_id = bank["id"]
            sav = savings.state_of(bank_id)
            if sav["minutes"] <= 0:
                return self._json({"ok": False, "reason": "insufficient",
                                   "detail": "储蓄里还没有可消费的时间：先答题，或让家长奖励",
                                   "savings_minutes": 0}, 409)
            if minutes > sav["minutes"]:
                return self._json({"ok": False, "reason": "insufficient",
                                   "detail": "储蓄里只有 %s 分钟" % sav["minutes"],
                                   "savings_minutes": sav["minutes"]}, 409)
            st = schedule_of(mac)
            if st.get("limited") and st.get("free"):
                return self._json({"ok": False, "reason": "free_window",
                                   "detail": "现在是自由上网时段，不用消费储蓄",
                                   "schedule": schedule_public(st),
                                   "savings_minutes": sav["minutes"]}, 409)
            if st.get("limited") and not st.get("allowed"):
                # 3.50.0：时段只限制消费，所以时段外答题可以、消费不行
                return self._json({"ok": False, "reason": "outside_schedule",
                                   "detail": schedule.hint(st),
                                   "schedule": schedule_public(st),
                                   "savings_minutes": sav["minutes"]}, 403)
            left = minutes_left(load_grants().get(mac) or {})
            if left is None:
                return self._json({"ok": False, "reason": "already",
                                   "detail": "这台设备已经永久放行，不用再消费",
                                   "savings_minutes": sav["minutes"]}, 409)
            if left > 0:
                return self._json({"ok": False, "reason": "already",
                                   "detail": "这台设备现在还有 %s 分钟，用完再消费" % left,
                                   "savings_minutes": sav["minutes"]}, 409)
            try:
                rec = grant(ip, mac, bank_id, bank["name"], minutes, count_cap=False)
            except NotManaged as exc:
                return self._json({"ok": False, "reason": "not_managed",
                                   "detail": str(exc)}, 403)
            except schedule.OutsideSchedule as exc:
                return self._json({"ok": False, "reason": "outside_schedule",
                                   "detail": str(exc)}, 403)
            except Exception as exc:
                log("RELEASE-FAIL ip=%s mac=%s err=%s" % (ip, exc))
                return self._json({"ok": False, "reason": "router_error",
                                   "detail": str(exc)}, 502)
            granted = int(rec.get("minutes") or 0)
            try:
                names = ADAPTER.names_by_mac()
            except Exception:
                names = {}
            spent = savings.debit(bank_id, granted, kind="release",
                                  reason="释放 %s 分钟上网" % granted,
                                  by=names.get(mac, "") or mac)
            if not spent.get("ok"):
                # 正常情况下持锁不会发生；兜底回滚这次放行，不能只记日志
                log("RELEASE-DEBIT-FAIL ip=%s mac=%s want=%s err=%s，回滚放行"
                    % (ip, mac, granted, spent.get("reason")))
                try:
                    with _grants_lock:
                        g = load_grants()
                        r = dict(g.get(mac) or {})
                        r["expire_at"] = int(time.time())
                        r["reblocked"] = True
                        r["reblocked_at"] = time.time()
                        r["reblocked_reason"] = "release_debit_failed"
                        g[mac] = r
                        save_grants(g)
                    restrict_mac(mac, bank, "消费扣款失败回滚")
                except Exception as exc:
                    log("RELEASE-ROLLBACK-FAIL mac=%s err=%s" % (mac, exc))
                return self._json({
                    "ok": False, "reason": "debit_failed",
                    "detail": "扣减储蓄失败，请刷新后重试",
                    "savings_minutes": savings.state_of(bank_id)["minutes"],
                }, 409)
            balance = spent.get("minutes", 0)
            log("RELEASE ip=%s mac=%s bank=%s -%smin 余额=%s expire=%s"
                % (ip, mac, bank_id, granted, balance,
                   rec.get("expire_at") or "永久"))
            msg = ("已放行到 %s（本时段结束），可用 %s 分钟"
                   % (rec.get("ends_hhmm"), rec.get("minutes_left"))
                   if rec.get("ends_hhmm") else
                   ("已永久放行" if rec.get("expire_at") == 0
                    else "已放行 %s 分钟，可用 %s 分钟"
                         % (granted, rec.get("minutes_left"))))
            return self._json({
                "ok": True, "mac": mac, "minutes": granted,
                "expire_at": rec.get("expire_at"), "now": int(time.time()),
                "remaining_minutes": rec.get("minutes_left"),
                "ends_hhmm": rec.get("ends_hhmm"),
                "savings_minutes": balance,
                "message": msg,
            })

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
    # 3.18.0：学情从「按设备 MAC」迁移到「按题库（人）」——同一套题库的
    # 多台设备合并成一个人；只需要成功一次，之后直接跳过。
    try:
        mapping = {mac: b["id"] for b in STORE.banks() for mac in b["devices"]}
        if learning.migrate_bank_keys(mapping):
            log("学情已迁移为「按题库（人）」：%s 个学习者"
                % len(learning.all_learners()))
    except Exception as exc:
        log("学情迁移失败（不影响使用）：%s" % exc)
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
