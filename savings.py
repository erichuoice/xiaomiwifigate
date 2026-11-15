#!/usr/bin/env python3
"""时间储蓄：答题所得与家长奖励先存起来，想上网时再按需「释放」（消费）。

- 答题通过（savings 模式）→ credit(kind="quiz")，设备保持受限，时间进余额
- 家长在后台首页填「行为 + 分钟」→ credit(kind="reward")
- 孩子点「消费」→ debit(kind="release") 成功后才真正放行设备对应分钟数

数据落在 state/savings.json（.gitignore 已排除 state/，随 state 一起备份）：
{
  "version": 1,
  "banks": {
    "bank_xxx": {
      "minutes": 45,              # 当前可消费余额
      "day": "2026-11-15",        # 下面两个计数按天归零
      "earned_today": 20,
      "spent_today": 15,
      "ledger": [                 # 最近 60 条，新的在前
        {"at": 1794, "minutes": 20, "kind": "quiz",
         "reason": "答对 10 题", "by": "iPhone-96"}
      ]
    }
  }
}

按「人」（题库 id）记账：同一学生的多台设备共用一个余额，换设备/代答都算同一个人。
"""

import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
PATH = os.path.join(STATE, "savings.json")

VERSION = 1
MAX_MINUTES = 100000            # 单账户余额上限，防手滑
MAX_ENTRY_MINUTES = 1440        # 单次存入 / 释放上限（一天）
LEDGER_KEEP = 60                # 每人最多留 60 条明细
LEDGER_SHOWN = 12               # 接口最多回显 12 条
PRESET_KEEP = 12                # 3.50.2：最近奖励行为最多留 12 个，做成下拉选项
EVENTS_KEEP = 5000              # 3.50.3：全局时间记录（奖惩/获取/消费），给「时间记录」页用

_lock = threading.RLock()


def _today(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value):
    return max(0, min(MAX_MINUTES, _int(value)))


def _blank():
    return {"version": VERSION, "banks": {}, "presets": [], "events": []}


def _normalize(data):
    out = _blank()
    if not isinstance(data, dict):
        return out
    banks = data.get("banks")
    if not isinstance(banks, dict):
        return out
    for bid, rec in banks.items():
        if not isinstance(rec, dict):
            continue
        bid = str(bid or "").strip()
        if not bid:
            continue
        item = {
            "minutes": _clamp(rec.get("minutes")),
            "day": str(rec.get("day") or ""),
            "earned_today": _clamp(rec.get("earned_today")),
            "spent_today": _clamp(rec.get("spent_today")),
            "ledger": [],
        }
        for entry in (rec.get("ledger") or [])[:LEDGER_KEEP]:
            if not isinstance(entry, dict):
                continue
            item["ledger"].append({
                "at": _int(entry.get("at")),
                "minutes": _int(entry.get("minutes")),
                "kind": str(entry.get("kind") or ""),
                "reason": str(entry.get("reason") or "")[:80],
                "by": str(entry.get("by") or "")[:40],
            })
        out["banks"][bid] = item
    # 3.50.2：最近用过的奖励行为（下拉选项），坏数据直接忽略
    seen = set()
    for raw in (data.get("presets") or [])[:PRESET_KEEP * 2]:
        if not isinstance(raw, dict):
            continue
        reason = str(raw.get("reason") or "").strip()[:40]
        if not reason or reason in seen:
            continue
        seen.add(reason)
        _pm = _int(raw.get("minutes"))
        _pm = max(-MAX_ENTRY_MINUTES, min(MAX_ENTRY_MINUTES, _pm)) or 10
        out["presets"].append({
            "reason": reason,
            "minutes": _pm,
            "count": max(1, _int(raw.get("count"))),
            "last": _int(raw.get("last")),
        })
        if len(out["presets"]) >= PRESET_KEEP:
            break
    # 3.50.3：全局时间记录（正数=获得，负数=扣除/消费）
    for raw in (data.get("events") or [])[:EVENTS_KEEP]:
        if not isinstance(raw, dict):
            continue
        mins = _int(raw.get("minutes"))
        if not mins:
            continue
        out["events"].append({
            "at": _int(raw.get("at")),
            "bank": str(raw.get("bank") or "")[:40],
            "minutes": mins,
            "kind": str(raw.get("kind") or "")[:20],
            "reason": str(raw.get("reason") or "")[:80],
            "by": str(raw.get("by") or "")[:40],
        })
    # 3.50.4：老数据只有每人 60 条的 ledger，没有全局 events。
    # 用 ledger 兜底补出时间记录（不覆盖已有 events，只在为空时补）。
    if not out["events"]:
        for bid, item in out["banks"].items():
            for entry in item.get("ledger") or []:
                mins = _int(entry.get("minutes"))
                if not mins:
                    continue
                out["events"].append({
                    "at": _int(entry.get("at")),
                    "bank": bid,
                    "minutes": mins,
                    "kind": str(entry.get("kind") or "")[:20],
                    "reason": str(entry.get("reason") or "")[:80],
                    "by": str(entry.get("by") or "")[:40],
                })
        out["events"].sort(key=lambda e: e["at"], reverse=True)
        out["events"] = out["events"][:EVENTS_KEEP]
    return out


def _load():
    if not os.path.exists(PATH):
        return _blank()
    try:
        with open(PATH, encoding="utf-8") as f:
            return _normalize(json.load(f))
    except (OSError, ValueError):
        return _blank()


def _save(data):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)


def _roll(rec, now):
    """跨天把「今日获得 / 今日已用」归零，余额与明细保留。"""
    day = _today(now)
    if rec.get("day") != day:
        rec["day"] = day
        rec["earned_today"] = 0
        rec["spent_today"] = 0
    return rec


def _rec(data, bank_id, now):
    bid = str(bank_id or "").strip()
    rec = data["banks"].get(bid)
    if not isinstance(rec, dict):
        rec = {"minutes": 0, "day": "", "earned_today": 0,
               "spent_today": 0, "ledger": []}
        data["banks"][bid] = rec
    return _roll(rec, now)


def _push(rec, now, minutes, kind, reason, by):
    rec["ledger"].insert(0, {
        "at": int(now),
        "minutes": int(minutes),
        "kind": str(kind or ""),
        "reason": str(reason or "")[:80],
        "by": str(by or "")[:40],
    })
    del rec["ledger"][LEDGER_KEEP:]


def _event(data, now, bank_id, minutes, kind, reason, by):
    """3.50.3：append 一条全局时间记录（不依赖每人 60 条的 ledger）。"""
    data.setdefault("events", []).insert(0, {
        "at": int(now),
        "bank": str(bank_id or "")[:40],
        "minutes": int(minutes),
        "kind": str(kind or "")[:20],
        "reason": str(reason or "")[:80],
        "by": str(by or "")[:40],
    })
    del data["events"][EVENTS_KEEP:]


def _public(rec):
    return {
        "minutes": rec.get("minutes", 0),
        "earned_today": rec.get("earned_today", 0),
        "spent_today": rec.get("spent_today", 0),
        "ledger": list(rec.get("ledger") or [])[:LEDGER_SHOWN],
    }


def state_of(bank_id):
    """一个人（题库）的储蓄快照；没有记录时返回零值，不会建文件。"""
    with _lock:
        data = _load()
        rec = data["banks"].get(str(bank_id or "").strip())
        if not isinstance(rec, dict):
            return {"minutes": 0, "earned_today": 0, "spent_today": 0, "ledger": []}
        return _public(_roll(rec, time.time()))


def all_states():
    """后台首页用：{bank_id: 快照}。"""
    with _lock:
        data = _load()
        now = time.time()
        return {bid: _public(_roll(rec, now)) for bid, rec in data["banks"].items()
                if isinstance(rec, dict)}


def today_earned(bank_id):
    return state_of(bank_id).get("earned_today", 0)


def credit(bank_id, minutes, kind="quiz", reason="", by="", cap=0,
           count_toward_day=True):
    """存入分钟数，返回操作结果。

    cap > 0 时按「今日累计获得」限流（和原来的每日上限同一个口径）：
    超出部分不发，added 可能小于请求值；连一分钟都发不出时 ok=False。
    count_toward_day=False 用于家长奖励：奖励是家长意志，不占孩子的每日额度。
    """
    minutes = _int(minutes)
    if minutes <= 0:
        return {"ok": False, "reason": "bad_request", "detail": "分钟数必须大于 0"}
    cap = max(0, _int(cap))
    now = time.time()
    with _lock:
        data = _load()
        rec = _rec(data, bank_id, now)
        added = min(minutes, MAX_ENTRY_MINUTES)
        if cap > 0:
            added = min(added, max(0, cap - rec["earned_today"]))
        if added <= 0:
            return {"ok": False, "reason": "daily_cap", "added": 0,
                    "minutes": rec["minutes"],
                    "earned_today": rec["earned_today"],
                    "daily_cap_minutes": cap}
        rec["minutes"] = min(MAX_MINUTES, rec["minutes"] + added)
        if count_toward_day:
            rec["earned_today"] += added
        _push(rec, now, added, kind, reason, by)
        _event(data, now, bank_id, added, kind, reason, by)
        _save(data)
        out = _public(rec)
        out.update({"ok": True, "added": added, "asked": minutes,
                    "clamped": added < min(minutes, MAX_ENTRY_MINUTES)})
        return out


def debit(bank_id, minutes, kind="release", reason="", by="", allow_partial=False):
    """扣掉分钟数（释放上网 / 家长惩罚）。余额不足时不扣，返回 ok=False。

    allow_partial=True 用于家长「扣时间」：余额不够时把余额扣到 0 为止，
    返回 ok=True + spent=实际扣掉的分钟数，避免家长想扣却完全扣不动。
    """
    minutes = _int(minutes)
    if minutes <= 0:
        return {"ok": False, "reason": "bad_request", "minutes": state_of(bank_id)["minutes"]}
    now = time.time()
    with _lock:
        data = _load()
        rec = _rec(data, bank_id, now)
        if rec["minutes"] < minutes and not allow_partial:
            return {"ok": False, "reason": "insufficient",
                    "minutes": rec["minutes"], "needed": minutes}
        spent = min(minutes, rec["minutes"])
        if spent <= 0:
            return {"ok": False, "reason": "insufficient", "minutes": 0,
                    "needed": minutes, "spent": 0}
        rec["minutes"] -= spent
        rec["spent_today"] += spent
        _push(rec, now, -spent, kind, reason, by)
        _event(data, now, bank_id, -spent, kind, reason, by)
        _save(data)
        out = _public(rec)
        out.update({"ok": True, "spent": spent, "asked": minutes,
                    "partial": spent < minutes})
        return out


def remember_reward(reason, minutes):
    """3.50.2：把「奖惩原因 + 分钟数」记成下次可选的下拉项（同一个原因合并）。

    3.50.3：允许负数（扣时间），正负号一起记住。
    """
    reason = str(reason or "").strip()[:40]
    minutes = _int(minutes)
    minutes = max(-MAX_ENTRY_MINUTES, min(MAX_ENTRY_MINUTES, minutes)) or 10
    if not reason:
        return []
    now = int(time.time())
    with _lock:
        data = _load()
        presets = [p for p in data.get("presets") or []
                   if p.get("reason") != reason]
        presets.insert(0, {"reason": reason, "minutes": minutes,
                           "count": 1, "last": now})
        # 老原因再次使用时，把次数加回去
        old = next((p for p in data.get("presets") or []
                    if p.get("reason") == reason), None)
        if old:
            presets[0]["count"] = _int(old.get("count")) + 1
        data["presets"] = presets[:PRESET_KEEP]
        _save(data)
        return data["presets"]


def presets():
    """3.50.2：后台首页奖励下拉要用的最近行为列表（新的在前）。"""
    with _lock:
        data = _load()
    return [dict(p) for p in (data.get("presets") or [])]


def log(period="day", bank_id=None, now=None, limit=400):
    """3.50.3：时间记录（每天 / 每周 / 每月），给后台「时间记录」页用。

    返回 buckets（每个周期的获得/扣除/净变化，再拆答题/奖励/消费/惩罚），
    records（窗口内最近 limit 条事件，新的在前）和 totals。
    """
    period = period if period in ("day", "week", "month") else "day"
    limit = max(1, min(2000, _int(limit) or 400))
    now = float(now if now is not None else time.time())
    bid = str(bank_id or "").strip()
    with _lock:
        data = _load()
    events = [e for e in (data.get("events") or [])
              if not bid or e.get("bank") == bid]

    def blank(label, date):
        return {"label": label, "date": date, "earned": 0, "spent": 0,
                "net": 0, "quiz": 0, "reward": 0, "release": 0, "punish": 0}

    def apply(row, e):
        m = _int(e.get("minutes"))
        kind = str(e.get("kind") or "")
        if m >= 0:
            row["earned"] += m
            if kind == "quiz":
                row["quiz"] += m
            else:
                row["reward"] += m
        else:
            loss = -m
            row["spent"] += loss
            if kind == "release":
                row["release"] += loss
            else:
                row["punish"] += loss
        row["net"] = row["earned"] - row["spent"]

    lt = time.localtime(now)
    if period == "day":
        rows, index, earliest = [], {}, None
        for i in range(29, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            row = blank(d[5:], d)
            rows.append(row)
            index[d] = row
        earliest = rows[0]["date"]
    elif period == "week":
        rows, index = [], {}
        monday = now - lt.tm_wday * 86400
        for i in range(11, -1, -1):
            start = monday - i * 7 * 86400
            label = time.strftime("%m-%d", time.localtime(start)) + " 周"
            date = time.strftime("%Y-%m-%d", time.localtime(start))
            row = blank(label, date)
            rows.append(row)
            index[date] = row
        earliest = rows[0]["date"]
    else:
        rows, index = [], {}
        y, m = lt.tm_year, lt.tm_mon
        for i in range(11, -1, -1):
            yy, mm = y, m - i
            while mm <= 0:
                mm += 12
                yy -= 1
            label = "%d-%02d" % (yy, mm)
            row = blank(label, label)
            rows.append(row)
            index[label] = row
        earliest = rows[0]["date"] + "-01"

    start_ts = time.mktime(time.strptime(earliest + " 00:00:00",
                                         "%Y-%m-%d %H:%M:%S"))
    kept = []
    for e in events:
        ts = _int(e.get("at"))
        if ts < start_ts:
            continue
        lt2 = time.localtime(ts)
        if period == "day":
            key = time.strftime("%Y-%m-%d", lt2)
        elif period == "week":
            key = time.strftime("%Y-%m-%d",
                                time.localtime(ts - lt2.tm_wday * 86400))
        else:
            key = time.strftime("%Y-%m", lt2)
        row = index.get(key)
        if row is not None:
            apply(row, e)
        kept.append(e)
    kept.sort(key=lambda x: _int(x.get("at")), reverse=True)
    totals = {"earned": 0, "spent": 0, "net": 0}
    for r in rows:
        totals["earned"] += r["earned"]
        totals["spent"] += r["spent"]
    totals["net"] = totals["earned"] - totals["spent"]
    return {"period": period, "bank": bid,
            "buckets": rows, "records": kept[:limit], "totals": totals,
            "generated_at": now, "total_events": len(events)}


def export_all():
    """3.50.1：整份备份 JSON 里带上储蓄，换 NAS 恢复不会丢余额。"""
    with _lock:
        data = _load()
    return {"version": VERSION, "banks": data["banks"],
            "presets": data.get("presets") or [],
            "events": data.get("events") or []}


def import_all(raw):
    """导入备份里的 savings（没有就跳过）；返回导入的学生数。"""
    with _lock:
        data = _normalize(raw if isinstance(raw, dict) else {})
        _save(data)
        return {"banks": len(data["banks"])}


def drop_bank(bank_id):
    """删除学生时顺手清掉他的储蓄，避免同名新学生继承旧余额。"""
    bid = str(bank_id or "").strip()
    if not bid:
        return False
    with _lock:
        data = _load()
        if bid not in data["banks"]:
            return False
        data["banks"].pop(bid, None)
        _save(data)
        return True
