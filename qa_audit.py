#!/usr/bin/env python3
"""题目 AI 审核结果（state/qa_audit.json，.gitignore 已排除 state/）。

每道题（qid）只审核一次：有结果的题目再次被学生反馈时直接复用，不再调用 AI。
记录内容：
  qid / bank_id / question（题目快照） / status（pending/ok/bad/error/gone）
  answer_ok / confidence / analysis / suggested_answer / model / provider
  checked_at / deleted / deleted_at / feedback_ids / error

3.50.9：学生反馈题目 → 后台 AI 审核 → 答案有误就删题，否则保留并留存详细分析。
"""

import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
PATH = os.path.join(STATE, "qa_audit.json")
LIMIT = 3000

_lock = threading.RLock()


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "items": {}}


def _save(data):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, PATH)


def _trim(items):
    if len(items) <= LIMIT:
        return items
    # 先丢最旧的、已完成的记录；pending 的保留
    ordered = sorted(items.items(), key=lambda kv: kv[1].get("checked_at") or 0)
    for qid, _ in ordered[:max(0, len(ordered) - LIMIT)]:
        if items.get(qid, {}).get("status") != "pending":
            items.pop(qid, None)
    return items


def get(qid):
    qid = str(qid or "").strip()
    if not qid:
        return None
    with _lock:
        item = _load()["items"].get(qid)
        return json.loads(json.dumps(item)) if isinstance(item, dict) else None


def all_items():
    with _lock:
        return [json.loads(json.dumps(v)) for v in _load()["items"].values()]


def attach_feedback(qid, fid):
    qid, fid = str(qid or "").strip(), str(fid or "").strip()
    if not qid or not fid:
        return False
    with _lock:
        data = _load()
        item = data["items"].get(qid)
        if not isinstance(item, dict):
            return False
        ids = item.setdefault("feedback_ids", [])
        if fid not in ids:
            ids.append(fid)
            ids[:] = ids[-30:]
            _save(data)
        return True


def claim(qid, bank_id, question, fid):
    """抢占一道题的审核。返回 (claimed, item)。

    已有记录（含 pending / ok / bad / error / gone）时直接返回 False + 现有记录，
    调用方就不再调用 AI —— 满足「分析过一次的题目重复反馈不再送 AI」。
    """
    qid = str(qid or "").strip()
    if not qid:
        return False, None
    with _lock:
        data = _load()
        item = data["items"].get(qid)
        if isinstance(item, dict):
            ids = item.setdefault("feedback_ids", [])
            if fid and fid not in ids:
                ids.append(fid)
                ids[:] = ids[-30:]
                _save(data)
            return False, json.loads(json.dumps(item))
        item = {
            "qid": qid,
            "bank_id": str(bank_id or ""),
            "question": json.loads(json.dumps(question or {})),
            "status": "pending",
            "answer_ok": None,
            "confidence": "",
            "analysis": "",
            "suggested_answer": "",
            "model": "",
            "provider": "",
            "checked_at": 0,
            "deleted": False,
            "deleted_at": 0,
            "error": "",
            "feedback_ids": [fid] if fid else [],
        }
        data["items"][qid] = item
        data["items"] = _trim(data["items"])
        _save(data)
        return True, json.loads(json.dumps(item))


def finish(qid, **fields):
    qid = str(qid or "").strip()
    with _lock:
        data = _load()
        item = data["items"].get(qid)
        if not isinstance(item, dict):
            return None
        item.update(fields)
        item["checked_at"] = int(fields.get("checked_at") or time.time())
        _save(data)
        return json.loads(json.dumps(item))


def reset(qid):
    """管理员点「重新分析」：清掉旧结果，重新变成 pending。"""
    qid = str(qid or "").strip()
    with _lock:
        data = _load()
        item = data["items"].get(qid)
        if not isinstance(item, dict):
            return None
        item.update({"status": "pending", "error": "", "answer_ok": None})
        _save(data)
        return json.loads(json.dumps(item))
