#!/usr/bin/env python3
"""3.50.9：学生反馈题目 → AI 审核 → 答案有误删题 / 答案正确保留并记录分析。

设计：
- 每道题（qid）只送 AI 一次：结果存在 state/qa_audit.json，重复反馈直接复用；
- 没配 AI Key 时只把原因记在反馈上，不写审核缓存（配好 Key 后还能再分析）；
- AI 调用在后台线程里跑，不耽误学生提交反馈；
- AI 判定 answer_ok=false 就删题，并把这道题从所有学生的错题本里清掉；
- AI 判定正确则保留题目，分析全文留在审核记录里，后台反馈列表可展开查看。
"""

import time

import ai_bank
import feedback
import learning
import qa_audit


def _default_analyzer(cfg):
    return lambda question: ai_bank.analyze_question(question, cfg)


def review_feedback(store, fid, force=False, analyzer=None, cfg=None):
    """审核一条学生反馈（同步执行；服务端用后台线程调用它）。"""
    item = feedback.get(fid)
    if not item:
        return {"ok": False, "reason": "not_found"}
    qid = str(item.get("qid") or "").strip()
    bank_id = str(item.get("bank_id") or "").strip()
    if not qid or not bank_id:
        feedback.set_analysis_error(fid, "这条反馈没有题目信息，无法送 AI 审核")
        return {"ok": False, "reason": "no_question"}

    existing = qa_audit.get(qid)
    if existing and not force and existing.get("status") != "pending":
        qa_audit.attach_feedback(qid, fid)
        return {"ok": True, "cached": True, "audit": existing}
    if existing and existing.get("status") == "pending" and not force:
        qa_audit.attach_feedback(qid, fid)
        return {"ok": True, "pending": True, "audit": existing}
    if force:
        existing = qa_audit.reset(qid) or existing

    bank = store.bank(bank_id)
    question = None
    if bank:
        question = next((q for q in bank.get("questions") or []
                         if q.get("id") == qid), None)
    if question is None:
        if existing and existing.get("status") in ("ok", "bad"):
            qa_audit.attach_feedback(qid, fid)
            return {"ok": True, "cached": True, "audit": existing}
        qa_audit.claim(qid, bank_id, {"text": item.get("text") or ""}, fid)
        audited = qa_audit.finish(
            qid, status="gone", deleted=True, checked_at=time.time(),
            analysis="题目已不存在（可能在其他地方被删除），无法继续审核。")
        feedback.set_analysis_error(fid, "题目已不存在，无法送 AI 审核")
        return {"ok": False, "reason": "gone", "audit": audited}

    if cfg is None:
        cfg = ai_bank.load_config()
    if analyzer is None:
        if not str(cfg.get("api_key") or ""):
            feedback.set_analysis_error(
                fid, "还没配置 AI API Key：在「系统设置 → AI 出题连接」填 Key 后，"
                     "点这条反馈的「重新分析」即可（不会重复消耗）。")
            return {"ok": False, "reason": "no_key"}
        analyzer = _default_analyzer(cfg)

    claimed, audited = qa_audit.claim(qid, bank_id, question, fid)
    if not claimed:
        return {"ok": True, "cached": True, "audit": audited}

    try:
        result = analyzer(question)
    except Exception as exc:
        msg = str(exc)[:300] or type(exc).__name__
        audited = qa_audit.finish(qid, status="error", error=msg,
                                  checked_at=time.time(),
                                  model=str(cfg.get("model") or ""),
                                  provider=str(cfg.get("provider") or ""))
        feedback.set_analysis_error(fid, "AI 审核失败：%s（可点「重新分析」重试）" % msg)
        return {"ok": False, "reason": "ai_error", "audit": audited}

    answer_ok = result.get("answer_ok") is not False
    bad = not answer_ok
    deleted = False
    delete_error = ""
    if bad:
        try:
            deleted = bool(store.delete_question(bank_id, qid))
        except Exception as exc:
            delete_error = str(exc)[:200]
        if deleted:
            try:
                learning.forget_question(qid)
            except Exception:
                pass
        elif not delete_error:
            delete_error = "题目已不存在"

    audited = qa_audit.finish(
        qid,
        status="bad" if bad else "ok",
        answer_ok=answer_ok,
        confidence=str(result.get("confidence") or ""),
        analysis=str(result.get("analysis") or ""),
        suggested_answer=str(result.get("suggested_answer") or ""),
        model=str(result.get("model") or cfg.get("model") or ""),
        provider=str(result.get("provider") or cfg.get("provider") or ""),
        checked_at=time.time(),
        deleted=deleted,
        deleted_at=time.time() if deleted else 0,
        error=delete_error,
    )
    if not bad:
        feedback.set_analysis_error(fid, "")
    return {"ok": True, "cached": False, "audit": audited,
            "deleted": deleted, "answer_ok": answer_ok}
