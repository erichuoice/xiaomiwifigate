#!/usr/bin/env python3
"""答题页「题目有异议」反馈：学生点反馈 → 存这里 → 后台「学习情况」里看。

纯本地 JSON（state/feedback.json，已被 .gitignore 排除），不联网。
"""

import json
import os
import secrets
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
PATH = os.path.join(STATE, "feedback.json")
LIMIT = 300            # 最多保留多少条（超出丢最旧的已处理）

_lock = threading.RLock()


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(items):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)


def add(mac="", bank_id="", qid="", text="", note=""):
    """新增一条反馈；note 为空返回 None（没写内容就不记）。"""
    # 3.40.0：说明可以不写（学生点一下就反馈），记空字符串即可
    note = str(note or "").strip()[:300]
    item = {
        "id": "fb_" + secrets.token_hex(4),
        "ts": int(time.time()),
        "mac": str(mac or "").strip().lower(),
        "bank_id": str(bank_id or "").strip(),
        "qid": str(qid or "").strip()[:64],
        "text": str(text or "").strip()[:200],
        "note": note,
        "status": "open",
    }
    with _lock:
        items = _load()
        items.insert(0, item)
        # 超量时优先淘汰最旧的「已处理」
        if len(items) > LIMIT:
            done = [x for x in items if x.get("status") != "open"]
            for old in done[-(len(items) - LIMIT):]:
                items.remove(old)
            items = items[:LIMIT]
        _save(items)
    return item


def get(fid):
    fid = str(fid or "").strip()
    if not fid:
        return None
    with _lock:
        return next((x for x in _load() if x.get("id") == fid), None)


def set_analysis_error(fid, text):
    """3.50.9：AI 审核失败/未配置时，把原因记在反馈上（不阻塞学生反馈）。"""
    fid = str(fid or "").strip()
    with _lock:
        items = _load()
        hit = next((x for x in items if x.get("id") == fid), None)
        if hit is None:
            return False
        hit["analysis_error"] = str(text or "").strip()[:300]
        _save(items)
        return True


def listing(status=None, bank=None):
    with _lock:
        items = _load()
    if status:
        items = [x for x in items if x.get("status") == status]
    if bank:
        items = [x for x in items if x.get("bank_id") == bank]
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items


def open_count():
    return sum(1 for x in listing() if x.get("status") == "open")


def resolve(fid, status="done"):
    with _lock:
        items = _load()
        hit = next((x for x in items if x.get("id") == fid), None)
        if hit is None:
            return False
        hit["status"] = "open" if status == "open" else "done"
        _save(items)
        return True


def delete(fid):
    with _lock:
        items = _load()
        keep = [x for x in items if x.get("id") != fid]
        if len(keep) == len(items):
            return False
        _save(keep)
        return True
