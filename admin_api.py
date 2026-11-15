#!/usr/bin/env python3
"""后台 API：登录、多题库增删改、奖励档位、设备归属、导入导出。

除登录、登录态查询、模板下载外，所有接口都要求有效 session。
返回 (HTTP 状态码, JSON 对象或 Raw, Set-Cookie 值或 None)，由 server.py 发出。
"""

import csv
import io
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import ai_bank
import auto_bank
import importer
import learning
import progress
import router_adapter
import schedule
import feedback
import store
import tizhuang
import updater

COOKIE = "wifigate_admin"
COOKIE_RE = re.compile(r"wifigate_admin=([A-Za-z0-9_\-]+)")


class Raw:
    """需要原样返回的响应（CSV / JSON 下载）。"""

    def __init__(self, body, ctype, filename=None):
        self.body = body
        self.ctype = ctype
        self.filename = filename


TYPE_NAMES = {"single": "单选", "multi": "多选", "judge": "判断"}


def _fmt_ts(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _answer_text(item):
    """正确答案的可读文字：单选 A、多选 A,C、判断 正确/错误。"""
    answer = item.get("answer")
    options = list(item.get("options") or [])
    qtype = item.get("type") or "single"
    if qtype == "judge" and not options:
        options = ["正确", "错误"]
    idxs = answer if isinstance(answer, list) else [answer]
    out = []
    for i in idxs:
        try:
            i = int(i)
        except (TypeError, ValueError):
            out.append(str(i))
            continue
        if 0 <= i < len(options):
            out.append("ABCDEFGH"[i] if qtype != "judge" else str(options[i]))
        else:
            out.append("ABCDEFGH"[i] if 0 <= i < 26 else str(i))
    return "、".join(out) if qtype == "judge" else ",".join(out)


def _options_text(item):
    options = list(item.get("options") or [])
    if not options:
        return ""
    return " / ".join("%s. %s" % ("ABCDEFGH"[i] if i < 8 else str(i + 1), o)
                      for i, o in enumerate(options))


def mistakes_csv(rows, bank_name=""):
    """错题本导出 CSV（带 BOM，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["序号", "题库", "题型", "学段", "年级", "科目", "知识点",
                     "错次数", "最近答错", "应复习时间", "状态",
                     "题干", "选项", "正确答案", "解析", "题目ID"])
    for i, item in enumerate(rows, 1):
        writer.writerow([
            i, bank_name, TYPE_NAMES.get(item.get("type") or "single", "单选"),
            item.get("stage") or "", item.get("grade") or "",
            item.get("subject") or "", item.get("topic") or "",
            int(item.get("count") or 0), _fmt_ts(item.get("last_wrong")),
            _fmt_ts(item.get("due_at")), item.get("status") or "",
            item.get("text") or "", _options_text(item),
            _answer_text(item), item.get("explain") or "",
            item.get("qid") or item.get("id") or "",
        ])
    return buf.getvalue()


def cookie_token(headers):
    m = COOKIE_RE.search(headers.get("Cookie", "") or "")
    return m.group(1) if m else ""


def _body(raw):
    try:
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _bank_id(st, data):
    bid = str(data.get("bank_id") or data.get("id") or "").strip()
    if bid and st.bank(bid):
        return bid
    return st.first_bank()["id"]


def dispatch(st, adapter, method, path, headers, raw, peer):
    token = cookie_token(headers)
    authed = st.check_session(token)
    parsed = urlparse(path)
    path = parsed.path
    query = parse_qs(parsed.query)

    if path == "/api/admin/session" and method == "GET":
        return 200, {"ok": True, "authed": authed,
                     "password_set": bool(st.admin_password())}, None

    if path == "/api/admin/login" and method == "POST":
        try:
            new_token = st.login(_body(raw).get("password", ""), peer)
        except store.StoreError as exc:
            return 401, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True}, (
            "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
            % (COOKIE, new_token))

    # 模板下载不要求登录：里面只有格式说明，不含任何真实数据
    if path == "/api/admin/template.csv" and method == "GET":
        return 200, Raw(importer.template_csv(),
                        "text/csv; charset=utf-8",
                        "wifi-gate-question-template.csv"), None

    if not authed:
        return 401, {"ok": False, "detail": "请先登录后台"}, None

    if path == "/api/admin/logout" and method == "POST":
        st.logout(token)
        return 200, {"ok": True}, (
            "%s=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % COOKIE)

    if path == "/api/admin/state" and method == "GET":
        cfg = st.snapshot()
        return 200, {
            "ok": True,
            "mode": cfg["router"]["mode"],
            "model": cfg["router"].get("model", ""),
            "dry_run": bool(cfg["router"].get("dry_run")),
            "banks": st.banks(),
            "settings": cfg["settings"],
            "summary": st.summary(),
        }, None

    if path == "/api/admin/update" and method == "GET":
        return 200, dict(ok=True, **updater.status()), None

    if path == "/api/admin/update/check" and method == "POST":
        rec = updater.check()
        return 200, dict(ok=True, **updater.status(), checked=rec), None

    if path == "/api/admin/update/apply" and method == "POST":
        want = str(_body(raw).get("version") or "").strip()
        try:
            rec = updater.install(expect=want)
        except updater.UpdaterError as exc:
            return 502, {"ok": False, "detail": str(exc)}, None
        updater.schedule_restart()
        return 200, {"ok": True, "from": rec.get("from"), "to": rec.get("to"),
                     "files": rec.get("files"), "backup": rec.get("backup"),
                     "restarting": True}, None

    if path == "/api/admin/bank" and method == "POST":
        data = _body(raw)
        try:
            clean = st.save_bank(data.get("bank") or data)
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "bank": clean, "banks": st.banks(),
                     "summary": st.summary()}, None

    if path == "/api/admin/bank/delete" and method == "POST":
        try:
            summary = st.delete_bank(_body(raw).get("id", ""))
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "summary": summary, "banks": st.banks(),
                     "stats": st.summary()}, None

    if path == "/api/admin/bank/assign" and method == "POST":
        data = _body(raw)
        try:
            res = st.assign_device(data.get("mac", ""), data.get("bank_id", ""))
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "result": res, "summary": st.summary()}, None

    if path == "/api/admin/settings" and method == "POST":
        data = _body(raw)
        try:
            clean = st.save_settings(data.get("settings") or data)
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "settings": clean}, None

    if path == "/api/admin/devices" and method == "GET":
        cfg = st.snapshot()
        banks = st.banks()
        owner = {}
        for b in banks:
            for mac in b["devices"]:
                owner[mac] = b["id"]
        if cfg["router"]["mode"] != "miwifi":
            rows = []
            hint = ("当前是 mock 模式（不连路由器），看不到真实设备。"
                    "把 config.json 的 router.mode 改成 miwifi 并重启后即可分配。")
        else:
            try:
                data = json.loads(adapter.device_list_raw())
                rows = router_adapter.normalize_devices(data)
                hint = ""
            except Exception as exc:
                rows, hint = [], "设备列表读取失败：%s" % exc
        present = {r["mac"].lower() for r in rows}
        bank_by_id = {b["id"]: b for b in banks}
        for row in rows:
            row["bank_id"] = owner.get(row["mac"].lower(), "")
            b = bank_by_id.get(row["bank_id"]) or {}
            row["limit_mode"] = b.get("limit_mode") or "block"
            row["throttle_kbps"] = int(b.get("throttle_kbps") or 0)
        # 已分配但当前不在线的设备也列出来，避免「看不见的条目」
        for mac, bid in owner.items():
            if mac not in present:
                b = bank_by_id.get(bid) or {}
                rows.append({
                    "ip": [], "mac": mac, "name": "（当前不在线或未在列表）",
                    "type": None, "online": 0, "wan": None, "bank_id": bid,
                    "limit_mode": b.get("limit_mode") or "block",
                    "throttle_kbps": int(b.get("throttle_kbps") or 0),
                })
        return 200, {
            "ok": True, "devices": rows, "hint": hint,
            "banks": [{"id": b["id"], "name": b["name"], "enabled": b["enabled"],
                       "limit_mode": b.get("limit_mode", "block"),
                       "throttle_kbps": int(b.get("throttle_kbps") or 0)}
                      for b in banks],
        }, None

    # ---- 设备答题时段（周一~周日各自可配多个时间段）----

    if path == "/api/admin/schedules" and method == "GET":
        now = time.time()
        rules = st.schedules()
        status = {mac: schedule.window_state(rule, now)
                  for mac, rule in rules.items()}
        return 200, {
            "ok": True, "schedules": rules, "status": status,
            "server_now": now, "week_names": list(schedule.WEEK_NAMES),
        }, None

    if path == "/api/admin/schedule" and method == "POST":
        data = _body(raw)
        mac = str(data.get("mac") or "").strip().lower()
        if not mac:
            return 400, {"ok": False, "detail": "缺少设备 MAC"}, None
        if "schedule" not in data:
            return 400, {"ok": False, "detail": "缺少时段规则内容"}, None
        try:
            clean = st.save_schedule(mac, data.get("schedule"))
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {
            "ok": True, "mac": mac, "schedule": clean,
            "status": (schedule.window_state(clean, time.time())
                       if clean else None),
            "summary": st.summary(),
        }, None

    # ---- 题库导入 ----

    if path == "/api/admin/import" and method == "POST":
        data = _body(raw)
        try:
            parsed_q = importer.parse_text(
                data.get("text", ""), data.get("format", "auto"),
                str(data.get("stage") or ""))
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        bank_id = _bank_id(st, data)
        result = {
            "ok": True,
            "bank_id": bank_id,
            "format": parsed_q["format"],
            "parsed": len(parsed_q["questions"]),
            "errors": parsed_q["errors"],
            "sample": parsed_q["questions"][:5],
        }
        if data.get("preview"):
            return 200, result, None
        if not parsed_q["questions"]:
            result["ok"] = False
            result["detail"] = "没有可导入的有效题目，请检查报错行"
            return 400, result, None
        mode = "replace" if str(data.get("mode")) == "replace" else "append"
        try:
            saved = st.apply_questions(bank_id, parsed_q["questions"], mode=mode)
        except (store.StoreError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        result.update(saved)
        result["mode"] = mode
        result["banks"] = st.banks()
        return 200, result, None

    if path == "/api/admin/restore-builtin" and method == "POST":
        data = _body(raw)
        mode = "replace" if str(data.get("mode")) == "replace" else "append"
        try:
            saved = st.restore_builtin(_bank_id(st, data), mode=mode)
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        saved["mode"] = mode
        saved["banks"] = st.banks()
        return 200, dict(saved, ok=True), None

    if path == "/api/admin/backup/import" and method == "POST":
        data = _body(raw)
        try:
            summary = st.import_all(data.get("data") or data)
        except store.StoreError as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "summary": summary, "banks": st.banks()}, None

    if path == "/api/admin/export.json" and method == "GET":
        body = json.dumps(st.export_all(), ensure_ascii=False, indent=2)
        return 200, Raw(body, "application/json; charset=utf-8",
                        "wifi-gate-backup.json"), None

    if path == "/api/admin/export.csv" and method == "GET":
        bid = (query.get("bank") or [""])[0]
        bank = st.bank(bid) if bid else st.first_bank()
        if not bank:
            return 404, {"ok": False, "detail": "题库不存在"}, None
        name = "wifi-gate-%s.csv" % re.sub(r"[^\w\-]+", "", bank["id"])
        return 200, Raw(importer.bank_to_csv(bank["questions"]),
                        "text/csv; charset=utf-8", name), None

    # ---- 3.24.0：管理对象（题库）的出题范围与教学进度预览 ----
    if path == "/api/admin/progress" and method == "GET":
        bid = str((query.get("bank") or [""])[0]).strip()
        bank = st.bank(bid) if bid else st.first_bank()
        if not bank:
            return 404, {"ok": False, "detail": "题库不存在"}, None
        spec = ai_bank.scope_from_bank(bank)
        # 3.25.0：教材按「地区」自动推断，不再手填
        textbook = progress.textbook_for_region(spec.get("region"))
        p, textbook, plines = progress.resolve(
            spec, {"textbook": textbook,
                   "progress_auto": bank.get("progress_auto", True),
                   "term_scope": bank.get("term_scope") or ""})
        # 3.32.0：附带题庄「已学章节」摘要（元数据免费；网络失败不影响本接口）
        learned = None
        try:
            learned = tizhuang.scope_for_bank(bank, p)
        except Exception:
            learned = None
        return 200, {
            "ok": True, "bank_id": bank["id"], "bank_name": bank["name"],
            "region": bank.get("region") or "",
            "stage": bank.get("stage") or "", "grade": bank.get("grade") or "",
            "textbook": textbook, "progress_auto": bank.get("progress_auto", True),
            "term_scope": bank.get("term_scope") or "",
            "progress": p, "lines": plines,
            "textbooks": progress.textbook_options(),
            "learned": learned,
            "learned_text": tizhuang.scope_text(learned) if learned else "",
        }, None

    # ---- 3.32.0：题庄在线题库（按已学章节抽题 / 试用额度 / License） ----
    if path == "/api/admin/tizhuang" and method == "GET":
        bid = str((query.get("bank") or [""])[0]).strip()
        bank = st.bank(bid) if bid else st.first_bank()
        out = {"ok": True, "config": tizhuang.config_view()}
        try:
            out["quota"] = tizhuang.quota()
            out["scope"] = tizhuang.scope_for_bank(bank or {}, progress.current_progress())
            out["scope_text"] = tizhuang.scope_text(out["scope"])
            out["subjects"] = [{"name": x["subject"]} for x in out["scope"]["subjects"]]
        except tizhuang.TzError as exc:
            out["quota"] = {}
            out["scope"] = None
            out["detail"] = str(exc)
        return 200, out, None

    if path == "/api/admin/tizhuang/config" and method == "POST":
        data = _body(raw)
        saved = tizhuang.set_license(data.get("license"))
        return 200, {"ok": True, "config": tizhuang.config_view(),
                     "saved": saved}, None

    if path == "/api/admin/tizhuang/preview" and method == "POST":
        data = _body(raw)
        bank = st.bank(str(data.get("bank_id") or "")) or st.first_bank()
        if not bank:
            return 404, {"ok": False, "detail": "题库不存在"}, None
        try:
            res = tizhuang.pull_questions(
                bank, progress.current_progress(),
                count=int(data.get("count") or 10),
                subject=str(data.get("subject") or "").strip() or None)
        except (tizhuang.TzError, ValueError, TypeError) as exc:
            return 502, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "questions": res["questions"],
                     "scope": res["scope"], "scope_text": tizhuang.scope_text(res["scope"]),
                     "quota": res["quota"], "config": tizhuang.config_view()}, None

    if path == "/api/admin/tizhuang/import" and method == "POST":
        data = _body(raw)
        bank = st.bank(str(data.get("bank_id") or "")) or st.first_bank()
        if not bank:
            return 404, {"ok": False, "detail": "题库不存在"}, None
        questions = data.get("questions")
        if not isinstance(questions, list) or not questions:
            return 400, {"ok": False, "detail": "没有要加入的题目"}, None
        try:
            saved = st.apply_questions(bank["id"], questions, mode="append")
        except (store.StoreError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "added": saved.get("added"),
                     "total": saved.get("total"), "banks": st.banks()}, None

    # ---- AI 出题（3.6.0）：密钥只进 state/ai.json，响应一律打码 ----
    if path == "/api/admin/ai/config" and method == "GET":
        return 200, {"ok": True, "config": ai_bank.public_config()}, None

    if path == "/api/admin/ai/config" and method == "POST":
        try:
            cfg = ai_bank.save_config(_body(raw))
        except (ai_bank.AIError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, {"ok": True, "config": cfg}, None

    if path == "/api/admin/ai/test" and method == "POST":
        try:
            res = ai_bank.test_connection()
        except ai_bank.AIError as exc:
            return 502, {"ok": False, "detail": str(exc)}, None
        return 200, dict(res, ok=True), None

    if path == "/api/admin/ai/generate" and method == "POST":
        data = _body(raw)
        spec = dict(data.get("spec") if isinstance(data.get("spec"), dict) else data)
        spec.pop("api_key", None)
        spec.pop("questions", None)
        bid = data.get("bank_id") or spec.get("bank_id")
        bank = st.bank(bid) if bid else st.first_bank()
        if bank:
            # 3.24.0：出题范围（地区/学段/年级/教材/进度）挂在管理对象上，
            # 出题时自动带上，页面不用再填；显式传了就以传入的为准。
            merged = dict(ai_bank.scope_from_bank(bank))
            merged.update(spec)
            spec = merged
            # 3.32.0：把题庄里「已学章节」交给 prompt，避免出到没学过的内容
            try:
                _tz = tizhuang.scope_for_bank(bank, progress.current_progress())
                _wanted = set(spec.get("subjects") or []) or set(
                    [spec.get("subject")] if spec.get("subject") else [])
                _chapters = []
                for _s in _tz.get("subjects") or []:
                    if _wanted and _s["subject"] not in _wanted:
                        continue
                    _chapters.append("%s：%s" % (
                        _s["subject"],
                        "、".join(c["name"] for c in _s["learned"]) or "—"))
                if _chapters:
                    spec["scope_chapters"] = _chapters
            except Exception:
                pass
            spec.setdefault("avoid", [q["text"] for q in bank["questions"]][:600])
        if "star" not in spec and "difficulty" not in spec and bank:
            # 3.15.0：没指定难度时按这个题库（人）最近正确率自动定星级
            star = learning.bank_target_star(bank["id"])
            if star:
                spec["star"] = star
        # 3.18.0：学情按题库（人）算，不再选设备
        focus = bool(data.get("focus_weak", True)) and bank is not None
        try:
            if focus:
                result = ai_bank.generate_for_device(spec,
                                                     learning.ability(bank["id"]))
            else:
                result = ai_bank.generate(spec)
        except (ai_bank.AIError, TypeError, ValueError) as exc:
            return 502, {"ok": False, "detail": str(exc)}, None
        return 200, dict(result, ok=True), None

    if path == "/api/admin/ai/apply" and method == "POST":
        data = _body(raw)
        questions = data.get("questions")
        if not isinstance(questions, list) or not questions:
            return 400, {"ok": False, "detail": "没有可入库的题目，请先生成预览"}, None
        mode = "replace" if str(data.get("mode")) == "replace" else "append"
        try:
            saved = st.apply_questions(_bank_id(st, data), questions, mode=mode)
        except (store.StoreError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        return 200, dict(saved, ok=True, banks=st.banks()), None

    # ---- 学情：错题本 / 弱项 / 复习进度（3.18.0 起按题库=人，不按设备）----
    if path == "/api/admin/learning/wrong" and method == "GET":
        which = str((query.get("bank") or [""])[0] or
                    (query.get("mac") or [""])[0] or "").strip().lower()
        bank = st.bank(which) if which else st.first_bank()
        bid = which or (bank["id"] if bank else "")
        bank_name = bank["name"] if bank else bid
        rows = learning.wrong_book(bid)
        safe = re.sub(r"[^\w\-]+", "", bid or "mistakes") or "mistakes"
        fmt = str((query.get("format") or ["csv"])[0]).strip().lower()
        if fmt == "json":
            body = json.dumps({"bank_id": bid, "bank_name": bank_name,
                               "wrong": rows}, ensure_ascii=False, indent=2)
            return 200, Raw(body, "application/json; charset=utf-8",
                            "wifi-gate-mistakes-%s.json" % safe), None
        return 200, Raw(mistakes_csv(rows, bank_name),
                        "text/csv; charset=utf-8",
                        "wifi-gate-mistakes-%s.csv" % safe), None

    if path == "/api/admin/learning" and method == "GET":
        which = str((query.get("bank") or [""])[0] or
                    (query.get("mac") or [""])[0] or "").strip().lower()
        rows = []
        for b in st.banks():
            a = learning.ability(b["id"])
            rows.append({
                "bank_id": b["id"], "name": b["name"],
                "devices": len(b["devices"]),
                "total": a["total"], "accuracy": a["accuracy"],
                "difficulty": a["difficulty"], "target_star": a["target_star"],
                "recent_accuracy": a["recent_accuracy"],
                "rounds": a["rounds"], "perfect": a["perfect"],
                "last": a["last"],
                "wrong_count": a["wrong_count"], "due_count": a["due_count"],
                "weak": [w["topic"] for w in a["weak"][:3]],
                "weakest_subject": a.get("weakest_subject") or "",
            })
        known = {b["id"] for b in st.banks()}
        for who in learning.all_learners():
            if who in known:
                continue
            a = learning.ability(who)
            rows.append({
                "bank_id": who, "name": who + "（未对应题库）", "devices": 0,
                "total": a["total"], "accuracy": a["accuracy"],
                "difficulty": a["difficulty"], "target_star": a["target_star"],
                "recent_accuracy": a["recent_accuracy"],
                "rounds": a["rounds"], "perfect": a["perfect"],
                "last": a["last"],
                "wrong_count": a["wrong_count"], "due_count": a["due_count"],
                "weak": [w["topic"] for w in a["weak"][:3]],
                "weakest_subject": a.get("weakest_subject") or "",
            })
        return 200, {"ok": True, "banks": rows,
                     "detail": learning.ability(which) if which else None}, None

    if path == "/api/admin/learning/clear" and method == "POST":
        data = _body(raw)
        who = str(data.get("bank_id") or data.get("mac") or "").strip().lower()
        if not who:
            return 400, {"ok": False, "detail": "请指定要清空的题库"}, None
        learning.clear(who)
        return 200, {"ok": True}, None

    # ---- 3.34.0：学生答题页的「题目有异议」反馈 ----
    if path == "/api/admin/feedback" and method == "GET":
        items = feedback.listing()
        known = {b["id"]: b["name"] for b in st.banks()}
        for item in items:
            item["bank_name"] = known.get(item.get("bank_id") or "", "")
        return 200, {"ok": True, "items": items,
                     "open": sum(1 for x in items if x.get("status") == "open")}, None

    if path == "/api/admin/feedback/resolve" and method == "POST":
        data = _body(raw)
        ok = feedback.resolve(str(data.get("id") or ""),
                              "open" if data.get("status") == "open" else "done")
        if not ok:
            return 404, {"ok": False, "detail": "反馈不存在"}, None
        return 200, {"ok": True, "items": feedback.listing(),
                     "open": feedback.open_count()}, None

    if path == "/api/admin/feedback/delete" and method == "POST":
        data = _body(raw)
        ok = feedback.delete(str(data.get("id") or ""))
        if not ok:
            return 404, {"ok": False, "detail": "反馈不存在"}, None
        return 200, {"ok": True, "items": feedback.listing(),
                     "open": feedback.open_count()}, None

    # ---- 3.34.0：错题本 / 反馈里直接改题目 ----
    if path == "/api/admin/question/update" and method == "POST":
        data = _body(raw)
        bank = st.bank(str(data.get("bank_id") or "")) or st.first_bank()
        if not bank:
            return 404, {"ok": False, "detail": "题库不存在"}, None
        qid = str(data.get("qid") or "").strip()
        if not qid:
            return 400, {"ok": False, "detail": "缺少题号"}, None
        patch = data.get("question") if isinstance(data.get("question"), dict) else data
        try:
            clean = st.update_question(bank["id"], qid, patch)
        except (store.StoreError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "detail": str(exc)}, None
        learning.patch_wrong(qid, clean)
        return 200, {"ok": True, "question": clean, "banks": st.banks()}, None

    # ---- 自动补充新题（3.10.0：每个题库可以设置不同的自动出题要求）----
    if path.startswith("/api/admin/auto-bank"):
        def payload():
            return {"ok": True,
                    "rules": auto_bank.load_all(st),
                    "status": auto_bank.status(st),
                    "options": {"stages": ai_bank.STAGES,
                                "grades": ai_bank.GRADES,
                                "subjects": ai_bank.SUBJECTS},
                    "banks": [{"id": b["id"], "name": b["name"],
                               "questions": len(b["questions"])}
                              for b in st.banks()]}

        if path == "/api/admin/auto-bank" and method == "GET":
            return 200, payload(), None

        if path == "/api/admin/auto-bank" and method == "POST":
            try:
                auto_bank.save_rule(_body(raw), st=st)
            except auto_bank.AutoBankError as exc:
                return 400, {"ok": False, "detail": str(exc)}, None
            return 200, payload(), None

        if path == "/api/admin/auto-bank/run" and method == "POST":
            try:
                bank_id = auto_bank.resolve_bank_id(st, _body(raw).get("bank_id"))
                rule = auto_bank.load_rule(st, bank_id)
                if rule is None:
                    raise auto_bank.AutoBankError(
                        "该题库还没保存过自动出题设置，请先点「保存自动出题设置」")
                rec = auto_bank.run_once(st, rule, "manual")
            except auto_bank.AutoBankError as exc:
                return 409, {"ok": False, "detail": str(exc)}, None
            return 200, {"ok": rec.get("ok", False), "run": rec,
                         "status": auto_bank.status(st),
                         "detail": rec.get("detail")}, None

        if path == "/api/admin/auto-bank/remove" and method == "POST":
            try:
                auto_bank.remove_rule(
                    auto_bank.resolve_bank_id(st, _body(raw).get("bank_id")))
            except auto_bank.AutoBankError as exc:
                return 400, {"ok": False, "detail": str(exc)}, None
            return 200, payload(), None

        if path == "/api/admin/auto-bank/clear" and method == "POST":
            auto_bank.clear_runs(_body(raw).get("bank_id"))
            return 200, payload(), None

    return 404, {"ok": False, "detail": "接口不存在"}, None
