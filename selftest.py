#!/usr/bin/env python3
"""自检脚本：服务启动后运行 python3 selftest.py。

用 X-Forwarded-For 模拟设备（仅从 127.0.0.1 发起时生效），覆盖：
多题库与设备归属 / 档位选择（答 N 题换 M 分钟）/ 逐题即时反馈 /
一轮全对才放行 / 时长重新计时 / 每日上限 / 答错重来与锁定 /
后台鉴权 / 题库校验 / 导入导出 / 备份恢复。

测试会临时新建「自检题库」并改设置，结束时用备份接口整份还原。
"""

import http.cookiejar
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import router_adapter  # noqa: E402

HOST = os.environ.get("WIFIGATE_HOST", "http://127.0.0.1:8080")
SEED = random.randint(1, 250)
IP_MAIN = "10.%d.21.5" % SEED

with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))

passed = 0
failed = 0

TEST_BANK = {
    "name": "自检题库",
    "tiers": [{"questions": 1, "minutes": 30}, {"questions": 2, "minutes": 60}],
    "devices": [],
    "questions": [
        {"type": "single", "text": "自检题：2 + 3 = ?", "options": ["4", "5", "6"],
         "answer": 1, "explain": "2 + 3 = 5。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：6 × 7 = ?", "options": ["42", "36", "48"],
         "answer": 0, "explain": "6 × 7 = 42。", "stage": "小学", "subject": "数学",
         "star": 1},
    ],
}


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s %s" % (name, detail))
    else:
        failed += 1
        print("FAIL  %s %s" % (name, detail))


def _open(req, opener):
    if opener is None:
        return urllib.request.urlopen(req, timeout=10)
    return opener.open(req, timeout=10)


def call(path, data=None, ip=None, opener=None):
    headers = {"X-Forwarded-For": ip or IP_MAIN, "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(HOST + path, data=body, headers=headers)
    try:
        with _open(req, opener) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def admin(path, data=None, ip=None):
    code, raw = call(path, data, ip, _opener)
    return code, _maybe_json(raw)


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def answers_of(bank_id):
    """从后台读出某题库的正确答案，用来构造「必定答错」的选项。"""
    code, st = admin("/api/admin/state")
    for b in st.get("banks", []):
        if b["id"] == bank_id:
            return {q["id"]: q["answer"] for q in b["questions"]}
    return {}


def wrong_pick(q, right_map):
    right = right_map.get(q["id"], 0)
    right_list = right if isinstance(right, list) else [right]
    for i in range(len(q["options"])):
        if i not in right_list:
            return [i]
    return [0]


def start(tier, ip=None, target=None):
    payload = {"tier": tier}
    if target:
        payload["target"] = target
    return admin("/api/start", payload, ip)


def answer(qid, choices, ip=None):
    return admin("/api/answer", {"id": qid, "choices": choices}, ip)


def play(round_data, right_ids, ip=None):
    """按给定答案顺序答完一轮，返回最后一次响应。"""
    d = round_data
    last = {}
    guard = 0
    while guard < 40:
        guard += 1
        q = d.get("question")
        if not q:
            break
        right = right_ids.get(q["id"], 0)
        pick = right if isinstance(right, list) else [right]
        code, d = answer(q["id"], pick, ip)
        last = d
        if d.get("done") or not d.get("ok"):
            break
    return last


def main():
    global IP_MAIN
    try:
        code, raw = call("/api/health")
        health = json.loads(raw)
        check("服务可达", code == 200 and health.get("ok") is True,
              "模式=%s，绑定 %s" % (health.get("mode"), health.get("bind")))
        check("已加载题库", int(health.get("questions", 0)) >= 100,
              "%s 套 / %s 道" % (health.get("banks"), health.get("questions")))
    except Exception as exc:
        print("服务未启动：%s" % exc)
        print("请先运行 python3 server.py")
        sys.exit(1)

    target = health.get("lan_ip") or ""
    if health.get("mode") != "mock" and target and not target.startswith("127."):
        IP_MAIN = target       # 真实模式下用本机内网 IP，路由器才查得到 MAC

    # ---- 解析层单测 ----
    device_list = {
        "mac": "6A:B1:04:E6:C1:D8",
        "list": [
            {"mac": "1A:70:07:03:B4:A1", "name": "iPad", "type": 2,
             "authority": {"wan": 0},
             "ip": [{"downspeed": "0", "ip": "192.168.31.252"}]},
            {"mac": "84:D3:28:BC:79:2E", "name": "Huo-2", "type": 2,
             "authority": {"wan": 1}, "ip": [{"ip": "192.168.31.201"}]},
        ],
    }
    hit = router_adapter._find_mac_by_ip(device_list, "192.168.31.252")
    check("嵌套 IP 反查 MAC", hit == "1a:70:07:03:b4:a1", "MAC=%s" % hit)
    check("读取 authority.wan",
          router_adapter.find_authority_wan(device_list, "192.168.31.252") == 0)
    check("设备列表归一化",
          len(router_adapter.normalize_devices(device_list)) == 2)

    original_post = router_adapter._post
    try:
        router_adapter._post = lambda url, data, timeout=6: (
            '{"code":0,"token":"abc123def456",'
            '"url":"/cgi-bin/luci/;stok=abc123def456/web/home"}')
        rcfg = dict(CFG["router"], admin_password="unit-test", stok="")
        adapter = router_adapter.MiWifiAdapter(rcfg, lambda m: None)
        adapter._token_expired = True
        check("stok 失效后自动重登",
              adapter.get_stok(force_refresh=True) == "abc123def456")
        try:
            adapter._check_token('{"code":401,"msg":"Invalid token"}')
            check("401 识别为登录态失效", False)
        except router_adapter.TokenExpired:
            check("401 识别为登录态失效", True)
    finally:
        router_adapter._post = original_post

    # ---- 时长计算单测 ----
    import server as srv  # 只导入，不启动服务
    now = time.time()
    check("剩余时间按分钟向上取整",
          srv.minutes_left({"expire_at": now + 600}) == 10)
    check("永久放行返回 None", srv.minutes_left({"expire_at": 0}) is None)
    check("已过期返回 0", srv.minutes_left({"expire_at": now - 60}) == 0)
    check("今日累计按日期归零",
          srv.granted_today({"day": "2000-01-01", "today_minutes": 99}) == 0)

    # ---- 页面与鉴权 ----
    code, raw = call("/")
    check("答题页可访问", code == 200 and "上网先答题" in raw, "HTTP %s" % code)
    check("答题页含家长控制面板", "家长控制" in raw and "ctrl-card" in raw)
    code, raw = call("/admin")
    check("管理后台可访问", code == 200 and "管理后台" in raw, "HTTP %s" % code)

    code, res = admin("/api/admin/state")
    check("未登录访问后台被拒", code == 401, "HTTP %s" % code)

    code, res = admin("/api/admin/login", {"password": "definitely-wrong"},
                      ip="10.%d.99.9" % SEED)
    check("错误管理密码被拒", code == 401, "HTTP %s" % code)

    code, res = admin("/api/admin/login",
                      {"password": CFG["server"].get("admin_password", "")})
    check("正确管理密码登录", code == 200 and res.get("ok") is True)

    admin("/api/admin/unlock", {})     # 清掉上次自检留下的锁定，保证可重复运行

    code, st = admin("/api/admin/state")
    ok_state = code == 200 and isinstance(st.get("banks"), list) and st["banks"]
    check("登录后读取多题库", ok_state,
          "%s 套题库" % len(st.get("banks", [])))
    if not ok_state:
        print("后台不可用，后续自检中止")
        sys.exit(1)

    # 上一次异常中断可能留下「自检题库」，先清掉，保证起始状态干净
    for b in list(st["banks"]):
        if b["name"] == TEST_BANK["name"] and len(st["banks"]) > 1:
            admin("/api/admin/bank/delete", {"id": b["id"]})
            code, st = admin("/api/admin/state")

    orig_backup = st
    orig_banks = len(st["banks"])
    orig_settings = st["settings"]
    orig_cap = orig_settings["daily_cap_minutes"]

    grants_path = os.path.join(BASE, "state", "grants.json")
    grants_before = {}
    if os.path.exists(grants_path):
        try:
            grants_before = json.load(open(grants_path, encoding="utf-8"))
        except ValueError:
            grants_before = {}

    # ---- 设备识别与归属 ----
    code, info = admin("/api/quiz")
    mac = info.get("mac") or ""
    check("识别本机 MAC", code == 200 and bool(mac), "MAC=%s" % mac)
    if mac and os.path.exists(grants_path):
        try:
            grants_now = json.load(open(grants_path, encoding="utf-8"))
        except ValueError:
            grants_now = {}
        if mac in grants_now:
            grants_now.pop(mac, None)
            with open(grants_path, "w", encoding="utf-8") as f:
                json.dump(grants_now, f, ensure_ascii=False, indent=2)
    if info.get("need"):
        check("已归属设备返回题库", bool(info.get("bank", {}).get("name")),
              info.get("bank", {}).get("name", ""))
    elif info.get("pick_target"):
        check("不受控设备可代答", bool(info.get("targets")),
              "可选 %s 台" % len(info.get("targets") or []))
    else:
        check("未归属设备提示不受控", info.get("reason") == "not_managed")

    code, res = start(0)
    if info.get("need"):
        check("已归属时可开始答题", code == 200)
    elif info.get("pick_target"):
        check("代答必须指定目标设备", code == 403, "HTTP %s" % code)
    else:
        check("未归属设备不能开始答题", code == 403, "HTTP %s" % code)

    # 代答：指定目标设备后能正常开局，且题库取自目标设备
    if info.get("pick_target") and info.get("targets"):
        tgt = info["targets"][0]
        code, res = start(0, target=tgt["mac"])
        check("代答指定设备可开局", code == 200, "HTTP %s" % code)
        check("代答题库取自目标设备",
              res.get("bank", {}).get("name") == tgt["bank"]["name"],
              "%s" % res.get("bank", {}).get("name"))
        code, res = start(0, target="02:00:00:00:00:01")
        check("代答拒绝非受控设备", code == 403, "HTTP %s" % code)

    # ---- 新建题库并分配设备 ----
    code, res = admin("/api/admin/bank", dict(TEST_BANK))
    test_bank = res.get("bank") or {}
    check("新建题库", code == 200 and bool(test_bank.get("id")),
          "%s / %s 道" % (test_bank.get("name"), len(test_bank.get("questions", []))))

    code, res = admin("/api/admin/bank/assign",
                      {"mac": mac, "bank_id": test_bank.get("id", "")})
    check("设备分配到题库", code == 200 and res.get("result", {}).get("bank_id"),
          res.get("result", {}).get("bank_name", ""))

    # ---- 手动断网 / 放行（gatectl 的服务端接口）----
    code, res = admin("/api/admin/device/wan")
    rows = res.get("devices") or []
    check("手动开关读取设备列表", code == 200 and
          any(d.get("mac") == mac for d in rows),
          "%s 台受控设备" % len(rows))
    code, res = admin("/api/admin/device/wan", {"mac": mac, "wan": False})
    check("手动断网接口", code == 200 and
          res.get("record", {}).get("reblocked") is True,
          "HTTP %s" % code)
    code, res = admin("/api/admin/device/wan",
                      {"mac": mac, "wan": True, "minutes": 5})
    check("手动放行接口（5 分钟）", code == 200 and
          res.get("record", {}).get("minutes_left") == 5,
          "剩余 %s 分钟" % res.get("record", {}).get("minutes_left"))
    code, res = admin("/api/admin/device/wan", {"mac": "02:00:00:00:00:02",
                                                "wan": True})
    check("手动开关拒绝非受控设备", code == 404, "HTTP %s" % code)
    admin("/api/admin/device/wan", {"mac": mac, "wan": False})  # 恢复断网状态

    code, info = admin("/api/quiz")
    tiers = info.get("tiers") or []
    check("答题页拿到本题库档位", code == 200 and len(tiers) == 2,
          " / ".join("%s题→%s分" % (t["questions"], t["minutes"]) for t in tiers))
    check("初始剩余 0 分钟", info.get("remaining_minutes") == 0)

    code, res = start(9)
    check("非法档位被拒绝", code == 400, "HTTP %s" % code)

    # ---- 逐题作答：答错重来 ----
    code, d = start(1)
    check("按档位出题（2 题档）", code == 200 and d.get("total") == 2,
          "本轮 %s 题 / %s 分钟" % (d.get("total"), d.get("minutes")))
    if not (d.get("question") or {}).get("id"):
        print("出题失败，后续自检中止：%s" % (d.get("detail") or d))
        sys.exit(1)
    q1 = d.get("question") or {}
    check("题目不下发答案与解析", "answer" not in q1 and "explain" not in q1)

    right = answers_of(test_bank["id"])
    code, d = answer(q1["id"], wrong_pick(q1, right))
    check("答错即时反馈", code == 200 and d.get("correct") is False)
    check("答错给出正确答案与解析",
          d.get("answer") is not None and bool(d.get("explain")),
          "答案=%s" % d.get("answer"))
    q2 = d.get("question") or {}
    code, d = answer(q2["id"], [right[q2["id"]]])
    check("一轮有错不算通过", d.get("done") is True and d.get("all_correct") is False,
          "错 %s 次" % d.get("wrong_count"))
    check("答错后返回档位以便重来", d.get("tier") == 1)

    code, d = answer("not-a-real-question", [0])
    check("没有轮次时拒绝作答", code == 409, "HTTP %s" % code)

    # ---- 全对 → 按档位放行 ----
    code, d = start(1)
    last = play(d, right)
    check("全部答对触发放行", last.get("all_correct") is True,
          "共 %s 题" % last.get("total"))
    check("按所选档位给时长", last.get("minutes") == 60,
          "本次 +%s 分钟" % last.get("minutes"))
    check("累计剩余 60 分钟", last.get("remaining_minutes") == 60,
          "今日 %s 分钟" % last.get("today_minutes"))
    check("返回到期时间", bool(last.get("expire_at")), str(last.get("expire_at")))
    check("放行返回设备 MAC", last.get("mac") == mac)

    grants = json.load(open(grants_path, encoding="utf-8"))
    rec = grants.get(mac) or {}
    check("放行记录落盘", bool(rec.get("expire_at")),
          "余额 %s 分钟" % rec.get("minutes"))

    code, info = admin("/api/quiz")
    check("答题页显示剩余时间", info.get("remaining_minutes") in (59, 60),
          "%s 分钟" % info.get("remaining_minutes"))

    # ---- 再答一档 → 从现在重新计时（不叠加） ----
    code, d = start(0)
    check("选 1 题档", code == 200 and d.get("total") == 1 and d.get("minutes") == 30)
    last = play(d, right)
    check("再答一档从现在重新计时", last.get("remaining_minutes") in (29, 30),
          "剩余 %s 分钟（原 60 分钟不叠加）" % last.get("remaining_minutes"))

    # ---- 每日上限 ----
    code, res = admin("/api/admin/settings",
                      {"settings": {"daily_cap_minutes": 100,
                                    "note": orig_settings["note"]}})
    check("设置每日上限", code == 200 and res.get("settings", {}).get(
        "daily_cap_minutes") == 100)
    code, d = start(0)
    check("上限内仍可开始答题", code == 200, "今日已得 %s" % info.get("today_minutes"))
    last = play(d, right)
    check("超出上限时按剩余额度发放", last.get("minutes") == 10,
          "本次 +%s 分钟（剩余额度 10）" % last.get("minutes"))
    code, res = start(0)
    check("额度用完拒绝开始", code == 409 and res.get("reason") == "daily_cap",
          res.get("detail", ""))

    admin("/api/admin/settings",
          {"settings": {"daily_cap_minutes": orig_cap,
                        "note": orig_settings["note"]}})

    # ---- 题库校验 ----
    bad = dict(TEST_BANK, id=test_bank.get("id"),
               tiers=[{"questions": 99, "minutes": 30}])
    code, res = admin("/api/admin/bank", bad)
    check("档位题数超过题库被拒绝", code == 400, res.get("detail", "")[:40])
    bad2 = dict(TEST_BANK, id=test_bank.get("id"),
                tiers=[{"questions": 1, "minutes": 30}])
    del bad2["questions"]
    bad2["questions"] = [TEST_BANK["questions"][0], {"type": "single", "text": ""}]
    code, res = admin("/api/admin/bank", bad2)
    check("空题干被拒绝", code == 400, res.get("detail", "")[:40])

    code, res = admin("/api/admin/bank/delete", {"id": "bank_不存在"})
    check("删除不存在的题库被拒", code == 400, res.get("detail", "")[:30])

    # ---- 导入 / 导出 ----
    sample = ("题型,学段,年级,科目,题干,选项A,选项B,选项C,选项D,答案,解析,难度\n"
              "单选,小学,二年级,数学,自检题：7+8=?,13,14,15,16,C,7+8=15,1\n"
              "判断,小学,三年级,科学,自检题：月亮会发光。,正确,错误,,,错,反射太阳光,1\n")
    code, res = admin("/api/admin/import",
                      {"text": sample, "preview": True,
                       "bank_id": test_bank.get("id")})
    check("导入预览识别正确",
          code == 200 and res.get("parsed") == 2 and not res.get("errors"),
          "识别 %s 题" % res.get("parsed"))

    code, res = admin("/api/admin/import",
                      {"text": sample, "mode": "append",
                       "bank_id": test_bank.get("id")})
    check("导入指定题库成功", code == 200 and res.get("added") == 2,
          "该题库共 %s 题" % res.get("total"))

    code, res = admin("/api/admin/import",
                      {"text": sample, "mode": "append",
                       "bank_id": test_bank.get("id")})
    check("重复导入按题干去重",
          code == 200 and res.get("added") == 0 and res.get("skipped") == 2)

    code, res = admin("/api/admin/import",
                      {"text": "这不是表格", "format": "csv",
                       "bank_id": test_bank.get("id")})
    check("无法识别的数据被拒绝", code == 400, "HTTP %s" % code)

    code, raw = call("/api/admin/template.csv", opener=_opener)
    check("模板可下载", code == 200 and "题型" in raw and "题干" in raw,
          "%s 字节" % len(raw))

    code, raw = call("/api/admin/export.csv?bank=" + test_bank.get("id", ""),
                     opener=_opener)
    check("按题库导出 CSV", code == 200 and "自检题" in raw, "%s 字节" % len(raw))

    code, raw = call("/api/admin/export.json", opener=_opener)
    backup = _maybe_json(raw)
    check("备份包含全部题库",
          code == 200 and len(backup.get("banks") or []) >= 2,
          "%s 套" % len(backup.get("banks") or []))

    # ---- 解除归属 ----
    admin("/api/admin/bank/assign", {"mac": mac, "bank_id": ""})
    code, info = admin("/api/quiz")
    check("解除归属后不用答题", info.get("need") is False)
    code, res = start(0)
    check("不受控设备无法开始答题", code == 403, "HTTP %s" % code)

    # ---- 答错次数锁定（放最后，会锁 60 秒） ----
    admin("/api/admin/bank/assign",
          {"mac": mac, "bank_id": test_bank.get("id")})
    limit = int(CFG["server"]["max_wrong_per_window"])
    right = answers_of(test_bank["id"])
    for i in range(limit):
        code, d = start(0)
        q = d.get("question") or {}
        if not q:
            break
        code, r = answer(q["id"], wrong_pick(q, right))
        check("答错一轮不计通过 #%s" % (i + 1),
              r.get("done") is True and r.get("all_correct") is False,
              "剩余 %s 次" % r.get("remain"))
    code, res = start(0)
    check("超出错误次数后锁定", code == 429, "HTTP %s" % code)

    # ---- 还原 ----
    code, res = admin("/api/admin/backup/import", {"data": orig_backup})
    check("备份整份还原", code == 200 and res.get("ok") is True,
          str(res.get("summary")))
    code, st = admin("/api/admin/state")
    check("题库数量已还原", len(st.get("banks", [])) == orig_banks,
          "%s 套" % len(st.get("banks", [])))
    check("每日上限已还原",
          st["settings"]["daily_cap_minutes"] == orig_cap)

    try:
        with open(grants_path, "w", encoding="utf-8") as f:
            json.dump(grants_before, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    admin("/api/admin/unlock", {})
    code, res = admin("/api/admin/logout", {})
    code2, res2 = admin("/api/admin/state")
    check("退出登录后会话失效", code == 200 and code2 == 401)

    print("\n结果：%s 项通过，%s 项失败" % (passed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
