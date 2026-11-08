#!/usr/bin/env python3
"""后台 API：登录、多题库增删改、奖励档位、设备归属、导入导出。

除登录、登录态查询、模板下载外，所有接口都要求有效 session。
返回 (HTTP 状态码, JSON 对象或 Raw, Set-Cookie 值或 None)，由 server.py 发出。
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import importer
import router_adapter
import store

COOKIE = "wifigate_admin"
COOKIE_RE = re.compile(r"wifigate_admin=([A-Za-z0-9_\-]+)")


class Raw:
    """需要原样返回的响应（CSV / JSON 下载）。"""

    def __init__(self, body, ctype, filename=None):
        self.body = body
        self.ctype = ctype
        self.filename = filename


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
        for row in rows:
            row["bank_id"] = owner.get(row["mac"].lower(), "")
        # 已分配但当前不在线的设备也列出来，避免「看不见的条目」
        for mac, bid in owner.items():
            if mac not in present:
                rows.append({
                    "ip": [], "mac": mac, "name": "（当前不在线或未在列表）",
                    "type": None, "online": 0, "wan": None, "bank_id": bid,
                })
        return 200, {
            "ok": True, "devices": rows, "hint": hint,
            "banks": [{"id": b["id"], "name": b["name"], "enabled": b["enabled"]}
                      for b in banks],
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

    return 404, {"ok": False, "detail": "接口不存在"}, None
