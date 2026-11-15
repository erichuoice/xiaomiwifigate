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
    return {"version": VERSION, "banks": {}, "presets": []}


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
        out["presets"].append({
            "reason": reason,
            "minutes": max(1, min(MAX_ENTRY_MINUTES, _int(raw.get("minutes")))),
            "count": max(1, _int(raw.get("count"))),
            "last": _int(raw.get("last")),
        })
        if len(out["presets"]) >= PRESET_KEEP:
            break
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
        _save(data)
        out = _public(rec)
        out.update({"ok": True, "added": added, "asked": minutes,
                    "clamped": added < min(minutes, MAX_ENTRY_MINUTES)})
        return out


def debit(bank_id, minutes, kind="release", reason="", by=""):
    """扣掉分钟数（释放上网时调用）。余额不足时不扣，返回 ok=False。"""
    minutes = _int(minutes)
    if minutes <= 0:
        return {"ok": False, "reason": "bad_request", "minutes": state_of(bank_id)["minutes"]}
    now = time.time()
    with _lock:
        data = _load()
        rec = _rec(data, bank_id, now)
        if rec["minutes"] < minutes:
            return {"ok": False, "reason": "insufficient",
                    "minutes": rec["minutes"], "needed": minutes}
        rec["minutes"] -= minutes
        rec["spent_today"] += minutes
        _push(rec, now, -minutes, kind, reason, by)
        _save(data)
        out = _public(rec)
        out.update({"ok": True, "spent": minutes})
        return out


def remember_reward(reason, minutes):
    """3.50.2：把「奖励原因 + 分钟数」记成下次可选的下拉项（同一个原因合并）。"""
    reason = str(reason or "").strip()[:40]
    minutes = max(1, min(MAX_ENTRY_MINUTES, _int(minutes)))
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


def export_all():
    """3.50.1：整份备份 JSON 里带上储蓄，换 NAS 恢复不会丢余额。"""
    with _lock:
        data = _load()
    return {"version": VERSION, "banks": data["banks"],
            "presets": data.get("presets") or []}


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
