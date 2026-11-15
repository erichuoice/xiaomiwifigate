#!/usr/bin/env python3
"""错题分析：把错题本按学科 / 知识点 / 题型归类，给出可能的原因与建议。

家长在后台「错题分析」页、孩子在答题页的「我的错题本」页都用这份数据。
"""

import time

import learning


def _type_cn(qtype):
    return {"single": "单选", "multi": "多选", "judge": "判断"}.get(qtype or "", "题目")


def analyze(bank, now=None, limit=60):
    """返回 {summary, subjects, topics, types, causes, items}。

    bank 是题库（含 questions），错题本按 bank id 取；题目优先用题库里的最新内容，
    题库里没有的（已删题）用错题本当时保存的副本。
    """
    bank = bank or {}
    now = float(now or time.time())
    who = bank.get("id") or ""
    rows = learning.wrong_book(who, now) if who else []
    by_id = {q.get("id"): q for q in (bank.get("questions") or [])}
    items = []
    for w in rows:
        q = by_id.get(w.get("qid")) or {}
        items.append({
            "qid": w.get("qid") or "",
            "text": q.get("text") or w.get("text") or "",
            "type": q.get("type") or w.get("type") or "single",
            "options": q.get("options") or w.get("options") or [],
            "answer": q.get("answer", w.get("answer")),
            "explain": q.get("explain") or w.get("explain") or "",
            "topic": q.get("topic") or w.get("topic") or "",
            "chapter": q.get("chapter") or "",
            "subject": q.get("subject") or w.get("subject") or "",
            "star": q.get("star", 2),
            "count": int(w.get("count") or 1),
            "last_wrong": int(w.get("last_wrong") or 0),
            "due_at": int(w.get("due_at") or 0),
            "due": bool(w.get("due")),
            "in_bank": bool(q),
        })
    items.sort(key=lambda x: (-x["count"], -x["last_wrong"]))
    shown = items[:max(1, int(limit))]

    topics, subjects, types = {}, {}, {}
    for it in items:
        key = it["topic"] or "未标注知识点"
        row = topics.setdefault(key, {"topic": key, "wrong_count": 0,
                                      "due": 0, "subjects": set()})
        row["wrong_count"] += it["count"]
        row["due"] += 1 if it["due"] else 0
        if it["subject"]:
            row["subjects"].add(it["subject"])
        key2 = it["subject"] or "未标注学科"
        srow = subjects.setdefault(key2, {"subject": key2, "wrong_count": 0,
                                          "due": 0, "topics": set()})
        srow["wrong_count"] += it["count"]
        srow["due"] += 1 if it["due"] else 0
        if it["topic"]:
            srow["topics"].add(it["topic"])
        trow = types.setdefault(it["type"], {"type": it["type"], "count": 0})
        trow["count"] += 1
    for row in topics.values():
        row["subjects"] = sorted(row["subjects"])
    for row in subjects.values():
        row["topics"] = sorted(row["topics"])[:3]
    topic_list = sorted(topics.values(),
                        key=lambda x: (-x["wrong_count"], x["topic"]))
    subject_list = sorted(subjects.values(),
                          key=lambda x: (-x["wrong_count"], x["subject"]))
    type_list = sorted(types.values(), key=lambda x: (-x["count"], x["type"]))

    # 学科正确率（借用学情统计，找出最弱的一门）
    stats = learning.subject_stats(who, now) if who else {"subjects": [], "weakest": None}
    subject_acc = {s["subject"]: s for s in stats.get("subjects") or []}
    for row in subject_list:
        acc = subject_acc.get(row["subject"]) or {}
        row["accuracy"] = acc.get("accuracy")
        row["total"] = acc.get("total", 0)

    high = [it for it in items if it["count"] >= 2]
    due = [it for it in items if it["due"]]
    recent = [it for it in items if it["last_wrong"] >= now - 7 * 86400]
    summary = {
        "total": len(items),
        "high_freq": len(high),
        "due": len(due),
        "topics": len(topic_list),
        "subjects": len(subject_list),
        "recent": len(recent),
    }

    causes = []
    if not items:
        causes.append("还没有错题记录，保持得不错。")
    else:
        if high:
            hot = sorted(high, key=lambda x: -x["count"])[:3]
            names = "、".join("「%s」" % (h["topic"] or "未标注") for h in hot)
            causes.append("有 %d 道题错过不止一次，集中在 %s：说明这些知识点还没真正掌握，"
                          "建议先看解析、回到课本复习该知识点，再重新做一遍这些题。"
                          % (len(high), names))
        if stats.get("weakest"):
            weak = subject_acc.get(stats["weakest"]) or {}
            acc = weak.get("accuracy")
            causes.append("「%s」是相对最弱的学科（正确率 %s，答过 %d 题），"
                          "错题里也有它：这一门可以多花一点时间。"
                          % (stats["weakest"],
                             "—" if acc is None else "%d%%" % round(acc * 100),
                             weak.get("total", 0)))
        if type_list:
            top = type_list[0]
            if top["count"] >= max(2, len(items) * 0.6):
                hints = {
                    "single": "单选错得多，常见原因是概念记混、审题太快——做题时把每个选项都看完再选。",
                    "multi": "多选错得多，常常是漏选或多选——注意题目里「至少」「除了」「不正确」这类词。",
                    "judge": "判断错得多，常见原因是对「一定」「都」「只有」这类绝对说法不敏感——"
                             "遇到绝对词先想反例。",
                }
                causes.append("错题以「%s」为主（%d 道）：%s"
                              % (_type_cn(top["type"]), top["count"],
                                 hints.get(top["type"], "可以针对性多练这一类题。")))
        if len(recent) >= 5:
            causes.append("最近 7 天新增了 %d 道错题，可能是这段时间学的内容比较难，"
                          "或者做题时比较急；可以先陪孩子把这几道题的解析讲一遍。" % len(recent))
        if due:
            causes.append("有 %d 道错题到了该复习的时间（错题本会按 2 小时 / 1 天 / 3 天 / "
                          "7 天 / 15 天安排重做），让孩子再答一轮会自动抽到它们。"
                          % len(due))
        if not any(c.startswith("「") for c in causes) and len(causes) < 2:
            causes.append("错题比较分散，没有明显集中的知识点；保持每天练一点即可。")

    return {
        "bank_id": who,
        "bank_name": bank.get("name", ""),
        "summary": summary,
        "subjects": subject_list,
        "topics": topic_list,
        "types": [{"type": t["type"], "name": _type_cn(t["type"]),
                   "count": t["count"]} for t in type_list],
        "causes": causes,
        "wrong": shown,
        "wrong_id": stats.get("weakest") or "",
        "generated_at": int(now),
    }
