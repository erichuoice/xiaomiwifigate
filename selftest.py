#!/usr/bin/env python3
"""自检脚本：服务启动后运行 python3 selftest.py。

用 X-Forwarded-For 模拟设备（仅从 127.0.0.1 发起时生效），覆盖：
多题库与设备归属 / 档位选择（答 N 题换 M 分钟）/ 逐题即时反馈 /
判卷规则（严格全对 + 正确率达标两种模式，3.11.0）/ 答错当场加题 /
时长重新计时 / 每日上限 / 答错重来与锁定 /
后台鉴权 / 题库校验 / 导入导出 / 备份恢复。

测试会临时新建「自检题库」并改设置，结束时用备份接口整份还原。
"""

import http.cookiejar
import json
import os
import random
import re
import shutil
import sys
import time
import tempfile
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import ai_bank  # noqa: E402
import auto_bank  # noqa: E402
import learning  # noqa: E402
import progress  # noqa: E402
import router_adapter  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

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
        # 3.11.0：答错加题需要题库有富余题，扩到 8 道
        {"type": "single", "text": "自检题：3 + 4 = ?", "options": ["7", "8", "9"],
         "answer": 0, "explain": "3 + 4 = 7。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：9 - 5 = ?", "options": ["3", "4", "5"],
         "answer": 1, "explain": "9 - 5 = 4。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：2 × 5 = ?", "options": ["10", "12", "14"],
         "answer": 0, "explain": "2 × 5 = 10。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：12 ÷ 3 = ?", "options": ["3", "4", "6"],
         "answer": 1, "explain": "12 ÷ 3 = 4。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：15 - 8 = ?", "options": ["6", "7", "8"],
         "answer": 1, "explain": "15 - 8 = 7。", "stage": "小学", "subject": "数学",
         "star": 1},
        {"type": "single", "text": "自检题：4 × 4 = ?", "options": ["14", "16", "18"],
         "answer": 1, "explain": "4 × 4 = 16。", "stage": "小学", "subject": "数学",
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

    # ---- 3.11.0 放行规则单测（纯校验函数，不碰真实数据）----
    s = store.validate_settings({}, {})
    check("放行规则默认：正确率 80% / 每答错加 2 题",
          s["pass_mode"] == "rate" and s["pass_rate_percent"] == 80 and
          s["wrong_add_questions"] == 2)
    s2 = store.validate_settings(
        {"pass_mode": "strict"}, {"pass_rate_percent": 70,
                                  "wrong_add_questions": 5})
    check("未提交的放行规则字段沿用当前值",
          s2["pass_mode"] == "strict" and s2["pass_rate_percent"] == 70 and
          s2["wrong_add_questions"] == 5)
    for name, patch in (("正确率阈值低于 50 被拒绝", {"pass_rate_percent": 40}),
                       ("正确率阈值超过 100 被拒绝", {"pass_rate_percent": 101}),
                       ("答错加题数为负被拒绝", {"wrong_add_questions": -1}),
                       ("答错加题数超过 10 被拒绝", {"wrong_add_questions": 11}),
                       ("未知的放行规则被拒绝", {"pass_mode": "随便填"})):
        try:
            store.validate_settings(patch, {})
            check(name, False)
        except store.StoreError:
            check(name, True)

    # ---- 3.12.0 放行规则存到题库上（校验函数，纯逻辑）----
    ok_bank = {"id": "bank_selftest", "name": "校验用题库",
               "tiers": [{"questions": 1, "minutes": 30}], "devices": [],
               "questions": [{"type": "single", "text": "自检：1 + 1 = ?",
                              "options": ["1", "2"], "answer": 1}]}
    b1 = store.validate_bank(dict(ok_bank, pass_mode="strict",
                                  pass_rate_percent=90, wrong_add_questions=0))
    check("题库可各自保存放行规则（严格 / 90% / 不加题）",
          b1["pass_mode"] == "strict" and b1["pass_rate_percent"] == 90 and
          b1["wrong_add_questions"] == 0)
    b2 = store.validate_bank(ok_bank, pass_default={
        "pass_mode": "rate", "pass_rate_percent": 65, "wrong_add_questions": 3})
    check("题库没带放行规则时沿用默认值（老数据升级行为不变）",
          b2["pass_mode"] == "rate" and b2["pass_rate_percent"] == 65 and
          b2["wrong_add_questions"] == 3)
    for name, patch in (("题库正确率阈值非法被拒绝", {"pass_rate_percent": 10}),
                        ("题库答错加题数非法被拒绝", {"wrong_add_questions": 99}),
                        ("题库未知放行模式被拒绝", {"pass_mode": "随便填"})):
        try:
            store.validate_bank(dict(ok_bank, **patch))
            check(name, False)
        except store.StoreError:
            check(name, True)

    # ---- 答题时段规则单测（纯函数、固定时间戳，不碰真实路由器）----
    # 2026-11-09 是周一，at(0)=周一、at(5)=周六、at(6)=周日
    def at(day, hour, minute=0):
        return time.mktime((2026, 11, 9 + day, hour, minute, 0, 0, 0, -1))

    week = schedule.normalize({
        "enabled": True,
        "days": ["18:00-20:00"] * 5 + ["09:00-22:00"] * 2,
    })
    check("时段规则规范化为周一到周日 7 天",
          len(week["days"]) == 7 and week["days"][0] == [["18:00", "20:00"]])
    check("工作日/周末相同的规则合并显示",
          "周一至周五" in schedule.summary(week) and "周六、周日" in schedule.summary(week))
    st = schedule.window_state(week, at(0, 19))
    check("周一 19:00 允许答题，20:00 结束",
          st["allowed"] and st["ends_hhmm"] == "20:00")
    st = schedule.window_state(week, at(0, 21))
    check("周一 21:00 禁止答题并给出下次时间",
          not st["allowed"] and st["next_label"] == "周二 18:00")
    st = schedule.window_state(week, at(5, 10))
    check("周六按周末规则 09:00-22:00 允许",
          st["allowed"] and st["ends_hhmm"] == "22:00")
    check("时段说明包含下次开放时间",
          "下次可答题" in schedule.hint(schedule.window_state(week, at(0, 21))))

    night = schedule.normalize({"enabled": True, "days": ["22:00-06:00"] * 7})
    st = schedule.window_state(night, at(0, 23))
    check("跨夜时段 23:00 允许且次日 06:00 结束",
          st["allowed"] and st["ends_hhmm"] == "06:00")
    check("跨夜时段次日凌晨 03:00 仍允许",
          schedule.window_state(night, at(1, 3))["allowed"])
    check("跨夜时段次日中午 12:00 禁止",
          not schedule.window_state(night, at(1, 12))["allowed"])

    full = schedule.normalize({"enabled": True, "days": ["00:00-24:00"] * 7})
    check("00:00-24:00 全天允许",
          schedule.window_state(full, at(2, 3))["allowed"])
    check("未配规则的设备不受限制",
          schedule.window_state(None, at(2, 3))["unlimited"])
    check("启用但整周留空 = 不限制",
          schedule.window_state(
              schedule.normalize({"enabled": True, "days": []}),
              at(2, 3))["unlimited"])
    check("停用规则 = 不限制",
          schedule.window_state(
              schedule.normalize({"enabled": False,
                                  "days": ["18:00-20:00"] * 7}),
              at(2, 3))["unlimited"])

    t = at(0, 19, 30)
    e, hh = schedule.clamp_expire(int(t + 600), week, t)
    check("时段内未超时的放行不截断", e == int(t + 600) and hh is None)
    e, hh = schedule.clamp_expire(int(t + 3600), week, t)
    check("超出本时段的放行被截到时段结束",
          e == int(at(0, 20)) and hh == "20:00")
    e, hh = schedule.clamp_expire(0, week, t)
    check("永久档位被截到本时段结束", e == int(at(0, 20)) and hh == "20:00")
    try:
        schedule.clamp_expire(int(t + 3600), week, at(0, 21))
        check("时段外拒绝放行", False, "没有抛异常")
    except schedule.OutsideSchedule:
        check("时段外拒绝放行", True)
    check("无规则时保留永久放行",
          schedule.clamp_expire(0, None, t) == (0, None))

    for bad in ("25:00-26:00", "18:00", "18:00-18:00", "abc"):
        try:
            schedule.parse_window(bad)
            good = False
        except schedule.ScheduleError:
            good = True
        check("拒绝非法时间段 %s" % bad, good)
    check("坏规则条目在整份加载时被忽略",
          len(schedule.normalize_all({
              "02:00:00:00:00:81": {"days": ["bad"]},
              "02:00:00:00:00:82": {"days": ["18:00-20:00"] * 7},
          })) == 1)

    # ---- 页面与鉴权 ----
    code, raw = call("/")
    check("答题页可访问", code == 200 and "上网先答题" in raw, "HTTP %s" % code)
    check("答题页无家长控制残留", "ctrl-card" not in raw)
    code, raw = call("/admin")
    check("管理后台可访问", code == 200 and "管理后台" in raw, "HTTP %s" % code)
    check("后台含断网/放行控制", "deviceWan" in raw and "已放行" in raw)
    check("后台每台设备都有按钮",
          "if (d.bank_id)" not in raw)

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

    # 3.6.0：自检会触碰 AI 配置与学情文件，跑完按原始字节还原
    extra_paths = {}
    for _name in ("ai.json", "learning.json", "auto_bank.json"):
        _p = os.path.join(BASE, "state", _name)
        extra_paths[_p] = (open(_p, "rb").read()
                           if os.path.exists(_p) else None)

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

    def set_pass_rule(**rule):
        """按题库设置放行规则（3.12.0：规则存在题库上，不再走全局设置）。"""
        code, cur_state = admin("/api/admin/state")
        b = next((x for x in (cur_state.get("banks") or [])
                  if x.get("id") == test_bank.get("id")), None)
        if not b:
            return 0, {"ok": False, "detail": "自检题库不存在"}
        b.update(rule)
        return admin("/api/admin/bank", b)

    code, res = admin("/api/admin/bank/assign",
                      {"mac": mac, "bank_id": test_bank.get("id", "")})
    check("设备分配到题库", code == 200 and res.get("result", {}).get("bank_id"),
          res.get("result", {}).get("bank_name", ""))

    # ---- 手动断网 / 放行（后台设备页按钮与 gatectl 的服务端接口）----
    code, res = admin("/api/admin/device/wan")
    rows = res.get("devices") or []
    check("手动开关读取设备列表", code == 200 and
          any(d.get("mac") == mac for d in rows),
          "%s 台设备" % len(rows))
    check("设备列表返回服务器时间（倒计时校准）",
          isinstance(res.get("server_now"), (int, float)) and
          res["server_now"] > 0,
          str(res.get("server_now")))
    code, res = admin("/api/admin/device/wan", {"mac": mac, "wan": False})
    check("手动断网接口", code == 200 and
          res.get("record", {}).get("reblocked") is True,
          "HTTP %s" % code)
    code, res = admin("/api/admin/device/wan",
                      {"mac": mac, "wan": True, "minutes": 5})
    check("手动放行接口（5 分钟）", code == 200 and
          res.get("record", {}).get("minutes_left") == 5,
          "剩余 %s 分钟" % res.get("record", {}).get("minutes_left"))
    # 暂时放行时，设备列表里要有可供前端倒计时的到期时间
    code, res = admin("/api/admin/device/wan")
    row = next((d for d in (res.get("devices") or []) if d["mac"] == mac), {})
    check("暂时放行带到期时间", bool(row) and
          row.get("expire_at", 0) > res.get("server_now", 0) and
          row.get("remaining_minutes") == 5,
          "expire=%s 剩余=%s 分" % (row.get("expire_at"),
                                    row.get("remaining_minutes")))
    code, res = admin("/api/admin/device/wan", {"mac": "02:00:00:00:00:02",
                                                "wan": True})
    check("手动开关拒绝不存在设备", code in (404, 502),
          "HTTP %s%s" % (code, "（路由器不可达，按拉取失败处理）"
                         if code == 502 else ""))
    admin("/api/admin/device/wan", {"mac": mac, "wan": False})  # 恢复断网状态

    code, info = admin("/api/quiz")
    tiers = info.get("tiers") or []
    check("答题页拿到本题库档位", code == 200 and len(tiers) == 2,
          " / ".join("%s题→%s分" % (t["questions"], t["minutes"]) for t in tiers))
    check("初始剩余 0 分钟", info.get("remaining_minutes") == 0)

    code, res = start(9)
    check("非法档位被拒绝", code == 400, "HTTP %s" % code)

    # ---- 3.12.0：放行规则存在题库上；本段先把自检题库切回严格模式 ----
    # 验证原有「全对才放行、答错整轮重来」的行为保持不变。
    code, res = set_pass_rule(pass_mode="strict")
    check("后台可按题库切回严格模式（全部答对才放行）",
          code == 200 and (res.get("bank") or {}).get("pass_mode") == "strict",
          str((res.get("bank") or {}).get("pass_mode")))

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

    # ---- 3.12.0 正确率放行：答错当场加题 + 达标放行（配在题库上）----
    code, res = set_pass_rule(pass_mode="rate", pass_rate_percent=80,
                              wrong_add_questions=2)
    check("题库可以配成正确率放行（80% / 每答错加 2 题）",
          code == 200 and (res.get("bank") or {}).get("pass_mode") == "rate"
          and (res.get("bank") or {}).get("pass_rate_percent") == 80
          and (res.get("bank") or {}).get("wrong_add_questions") == 2)

    # 给自检题库加一个 5 题档，让「答错还能达标」有稀释空间
    code, cur = admin("/api/admin/state")
    tb = next((b for b in cur.get("banks") or []
               if b.get("id") == test_bank.get("id")), {})
    tb["tiers"] = (tb.get("tiers") or []) + [{"questions": 5, "minutes": 30}]
    code, res = admin("/api/admin/bank", tb)
    check("题库新增 5 题档位",
          code == 200 and len((res.get("bank") or {}).get("tiers") or []) == 3)

    code, info = admin("/api/quiz")
    pr = info.get("pass_rule") or {}
    check("答题页按本题库下发放行规则（模式/阈值/加题数）",
          pr.get("mode") == "rate" and pr.get("rate") == 80 and pr.get("add") == 2,
          str(pr))
    tgt_pr = ((info.get("targets") or [{}])[0] or {}).get("pass_rule") or {}
    check("可选设备列表里每台设备带自己题库的放行规则",
          bool(tgt_pr) and tgt_pr == pr, str(tgt_pr))

    # 全局设置里的那份只作为新题库的默认值，不能盖掉题库自己的规则
    admin("/api/admin/settings", {"settings": {
        "pass_mode": "strict", "pass_rate_percent": 60,
        "wrong_add_questions": 0, "daily_cap_minutes": orig_cap,
        "note": orig_settings["note"]}})
    code, info = admin("/api/quiz")
    pr2 = info.get("pass_rule") or {}
    check("判卷按题库自己的规则，全局设置只作新题库默认值",
          pr2.get("mode") == "rate" and pr2.get("rate") == 80 and
          pr2.get("add") == 2, str(pr2))
    admin("/api/admin/settings", {"settings": {
        "pass_mode": "rate", "pass_rate_percent": 80,
        "wrong_add_questions": 2, "daily_cap_minutes": orig_cap,
        "note": orig_settings["note"]}})

    right = answers_of(test_bank["id"])

    # ① 答错当场加题：5 题档答错第 1 题 → 本轮 5 → 7 题，继续作答
    code, d = start(2)
    q1 = d.get("question") or {}
    code, d = answer(q1["id"], wrong_pick(q1, right))
    check("答错当场追加 2 题（本轮 5 → 7）",
          d.get("ok") is True and d.get("done") is False and
          d.get("added_questions") == 2 and d.get("total") == 7 and
          bool(d.get("question")), "加 %s 题 / 共 %s 题"
          % (d.get("added_questions"), d.get("total")))
    last = play(d, right)
    check("正确率 86% ≥ 80%，答错一题也能达标放行",
          last.get("done") is True and last.get("all_correct") is True and
          last.get("accuracy_percent") == 86 and last.get("minutes") == 30,
          "正确率 %s%%" % last.get("accuracy_percent"))
    check("达标放行同样按档位给时长",
          bool(last.get("expire_at")) and
          last.get("remaining_minutes") in (29, 30),
          "剩余 %s 分钟" % last.get("remaining_minutes"))

    # ② 不达标：2 题档答错 1 题 → 4 题里对 3 道 = 75% < 80%，需要重来
    code, d = start(1)
    q1 = d.get("question") or {}
    code, d = answer(q1["id"], wrong_pick(q1, right))
    check("小档位同样当场加题（2 → 4）",
          d.get("done") is False and d.get("added_questions") == 2 and
          d.get("total") == 4)
    last = play(d, right)
    check("75% < 80% 不算通过，需要重来",
          last.get("done") is True and last.get("all_correct") is False and
          last.get("accuracy_percent") == 75 and
          last.get("pass_rate_percent") == 80 and last.get("pass_mode") == "rate",
          "正确率 %s%% / 阈值 %s%%"
          % (last.get("accuracy_percent"), last.get("pass_rate_percent")))

    # ③ 边界：正确率恰好等于阈值也放行（≥ 判定）
    code, res = set_pass_rule(pass_rate_percent=75)
    check("题库阈值可以单独调成 75", code == 200 and
          (res.get("bank") or {}).get("pass_rate_percent") == 75)
    code, d = start(1)
    q1 = d.get("question") or {}
    code, d = answer(q1["id"], wrong_pick(q1, right))
    last = play(d, right)
    check("正确率恰好达到阈值也放行（≥ 判定）",
          last.get("all_correct") is True and last.get("accuracy_percent") == 75,
          "正确率 %s%%" % last.get("accuracy_percent"))
    set_pass_rule(pass_rate_percent=80)

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

    # ---- 答错次数锁定（放最后，会锁 60 秒）----
    admin("/api/admin/bank/assign",
          {"mac": mac, "bank_id": test_bank.get("id")})
    # 3.12.0：锁定语义按「整轮重来」验证，先把自检题库切回严格模式；
    # 前面正确率模式的用例已累计错误计数，这里清零保持用例独立。
    set_pass_rule(pass_mode="strict")
    code, res = admin("/api/admin/unlock", {})
    check("清零答题错误计数（后台一键解锁）",
          code == 200 and res.get("ok") is True)
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

    # ---- AI 出题与学情接口（3.6.0；题库改动由下面的整份还原清理）----
    code, cur_state = admin("/api/admin/state")
    ai_bank_id = (cur_state.get("banks") or [{}])[0].get("id", "")
    code, res = admin("/api/admin/ai/config")
    aicfg = res.get("config") or {}
    check("AI 配置接口返回服务商预设与当前进度",
          code == 200 and bool(aicfg.get("presets")) and
          bool((aicfg.get("progress") or {}).get("label")),
          (aicfg.get("progress") or {}).get("label", ""))
    check("AI 配置永不回显 api_key 字段",
          "api_key" not in (res.get("config") or {}))
    check("AI 配置提供广州教材版本选项", len(aicfg.get("textbooks") or []) >= 3)

    code, res = admin("/api/admin/ai/config", {
        "provider": "deepseek", "api_key": "sk-selftest-abcdefgh",
        "textbook": "人教版（全科目）", "progress_auto": False,
        "term_scope": "第 1—2 单元（自检用）",
    })
    aicfg = res.get("config") or {}
    check("后台填写 API Key 只回显后四位",
          code == 200 and aicfg.get("has_key") is True and
          aicfg.get("key_hint") == "sk-****efgh" and
          "sk-selftest" not in json.dumps(res, ensure_ascii=False),
          aicfg.get("key_hint", ""))
    check("手动教学进度范围保存到配置",
          aicfg.get("progress_auto") is False and
          (aicfg.get("progress") or {}).get("scope") == "第 1—2 单元（自检用）")
    code, res = admin("/api/admin/ai/config", {"api_key": "sk-****efgh"})
    check("打码 Key 不会覆盖已保存密钥",
          (res.get("config") or {}).get("key_hint") == "sk-****efgh")
    code, res = admin("/api/admin/ai/config", {"clear_key": True})
    check("清除 Key 后配置显示未配置",
          (res.get("config") or {}).get("has_key") is False)
    code, res = admin("/api/admin/ai/test", {})
    check("未配置 Key 时测试连接如实报错", code == 502 and
          "API Key" in (res.get("detail") or ""), res.get("detail", "")[:40])

    code, res = admin("/api/admin/ai/generate", {
        "spec": {"stage": "小学", "grade": "四年级", "subject": "数学",
                 "count": 6, "mode": "offline"},
        "bank_id": ai_bank_id, "focus_weak": False,
    })
    qs = res.get("questions") or []
    qtypes = {q.get("type") for q in qs}
    check("离线生成单选/多选/判断混合题",
          code == 200 and len(qs) == 6 and qtypes == {"single", "multi", "judge"},
          str(sorted(qtypes)))
    check("生成题带知识点且含广州本地情境",
          all(q.get("topic") and q.get("source") for q in qs) and
          any(any(w in q.get("text", "") for w in
                  ("广州", "珠江", "早茶", "陈家祠", "白云山")) for q in qs))
    check("生成结果携带当前教学进度",
          bool((res.get("progress") or {}).get("scope")))
    code, res = admin("/api/admin/ai/apply",
                      {"questions": [], "bank_id": ai_bank_id})
    check("没有预览题时入库被拒绝", code == 400)
    code, res = admin("/api/admin/ai/apply",
                      {"questions": qs[:2], "bank_id": ai_bank_id, "mode": "append"})
    check("预览题目可追加进题库", code == 200 and res.get("added") == 2,
          "新增 %s 道" % res.get("added"))

    code, res = admin("/api/admin/learning")
    check("学情接口返回设备汇总列表",
          code == 200 and isinstance(res.get("devices"), list))
    code, res = admin("/api/admin/learning?mac=" + (mac or "02:00:00:00:00:77"))
    detail = res.get("detail") or {}
    check("学情明细含正确率/弱项/错题字段",
          code == 200 and all(k in detail for k in
                              ("accuracy", "weak", "wrong", "due_count")))
    code, res = admin("/api/admin/learning/clear", {"mac": "02:00:00:00:00:77"})
    check("清空指定设备学情接口可用", code == 200 and res.get("ok") is True)
    code, res = admin("/api/admin/learning/clear", {})
    check("清空学情缺少 MAC 被拒绝", code == 400)

    # ---- 3.10.0 自动补题接口：每个题库一套规则（全程离线，不联网、不消耗 Key）----
    st_banks = cur_state.get("banks") or []
    if len(st_banks) < 2:            # 只有一套题库时临时造一套，验证完由还原清理
        seed_q = dict((st_banks[0].get("questions") or [{}])[0])
        seed_q["id"] = "junior_seed"
        code, res = admin("/api/admin/bank",
                          {"name": "自检第二题库", "questions": [seed_q]})
        junior_bank_id = (res.get("bank") or {}).get("id", "")
    else:
        junior_bank_id = st_banks[1]["id"]

    code, res = admin("/api/admin/auto-bank")
    ab_status = res.get("status") or {}
    check("自动出题接口返回规则列表/状态/题库/选项",
          code == 200 and res.get("ok") is True and
          all(k in res for k in ("rules", "status", "banks", "options")) and
          bool((res.get("options") or {}).get("stages")))
    check("状态卡按题库分行：每个题库都有未做过/阈值/下次触发",
          isinstance(ab_status.get("banks"), list) and
          len(ab_status["banks"]) >= 2 and
          all(all(k in row for k in
                  ("bank_id", "bank_name", "fresh", "threshold", "next_text",
                   "state_text", "subjects", "auto_questions", "configured"))
              for row in ab_status["banks"]))
    check("状态卡含合并运行记录与 Key 状态",
          isinstance(ab_status.get("runs"), list) and "has_key" in ab_status)
    code, res = admin("/api/admin/auto-bank", {"threshold": 2})
    check("非法触发阈值被后台拒绝",
          code == 400 and "阈值" in (res.get("detail") or ""))

    code, before = admin("/api/admin/state")
    bank_before = next((b for b in before.get("banks", [])
                        if b["id"] == ai_bank_id), {})
    manual_before = len([q for q in bank_before.get("questions", [])
                         if not str(q.get("source") or "").startswith("auto-")])

    code, res = admin("/api/admin/auto-bank", {
        "enabled": False, "threshold": 30, "cooldown_minutes": 5,
        "count": 3, "mode": "offline", "max_auto": 5,
        "bank_id": ai_bank_id, "stage": "小学", "grade": "四年级",
        "subjects": ["数学", "英语"], "topics": "",
        "mix": {"single": 1, "multi": 1, "judge": 1},
    })
    p_rule = next((r for r in res.get("rules") or []
                   if r["bank_id"] == ai_bank_id), {})
    check("保存小学库的自动出题规则（保持关闭，避免自检期间被触发）",
          code == 200 and p_rule.get("count") == 3 and
          p_rule.get("max_auto") == 5 and p_rule.get("enabled") is False)
    check("自动出题科目多选可保存并回读",
          p_rule.get("subjects") == ["数学", "英语"],
          str(p_rule.get("subjects")))

    code, res = admin("/api/admin/auto-bank", {
        "enabled": False, "threshold": 20, "cooldown_minutes": 10,
        "count": 5, "mode": "offline", "max_auto": 10,
        "bank_id": junior_bank_id, "stage": "初中", "grade": "八年级",
        "subjects": ["物理", "英语"], "topics": "浮力",
    })
    j_rule = next((r for r in res.get("rules") or []
                   if r["bank_id"] == junior_bank_id), {})
    p_rule2 = next((r for r in res.get("rules") or []
                    if r["bank_id"] == ai_bank_id), {})
    check("初中库可以保存另一套不同的要求（阈值/学段/科目/知识点）",
          code == 200 and j_rule.get("threshold") == 20 and
          j_rule.get("stage") == "初中" and j_rule.get("grade") == "八年级" and
          j_rule.get("subjects") == ["物理", "英语"] and
          j_rule.get("topics") == "浮力")
    check("保存初中库规则不影响小学库已存的规则",
          p_rule2.get("count") == 3 and p_rule2.get("threshold") == 30 and
          p_rule2.get("subjects") == ["数学", "英语"])
    code, res = admin("/api/admin/auto-bank")
    rows = {r["bank_id"]: r for r in (res.get("status") or {}).get("banks") or []}
    check("状态表里两库各自的阈值/科目一目了然",
          rows.get(junior_bank_id, {}).get("threshold") == 20 and
          rows.get(ai_bank_id, {}).get("threshold") == 30 and
          rows.get(junior_bank_id, {}).get("subjects") == ["物理", "英语"])

    added_total, run_ok = 0, True
    for _i in range(3):
        code, res = admin("/api/admin/auto-bank/run", {"bank_id": ai_bank_id})
        run = res.get("run") or {}
        run_ok = run_ok and code == 200 and bool(run.get("ok"))
        added_total += int(run.get("added") or 0)
    check("立即试跑自动生成并直接入库，无需人工勾选题",
          run_ok and added_total >= 3, "累计入库 %s 道" % added_total)

    code, after = admin("/api/admin/state")
    bank_after = next((b for b in after.get("banks", [])
                       if b["id"] == ai_bank_id), {})
    qs_after = bank_after.get("questions", [])
    autos = [q for q in qs_after
             if str(q.get("source") or "").startswith("auto-")]
    manual_after = len(qs_after) - len(autos)
    check("自动题带来源标记且不超过上限",
          0 < len(autos) <= 5, "%s 道自动题" % len(autos))
    check("淘汰自动题不会动内置题 / 手动入库的题",
          manual_after == manual_before,
          "人工题 %s → %s" % (manual_before, manual_after))

    code, res = admin("/api/admin/auto-bank")
    ab_runs = (res.get("status") or {}).get("runs") or []
    check("每次执行都留下可追溯的运行记录（含所属题库）",
          len(ab_runs) >= 3 and
          all(r.get("reason") == "manual" and r.get("at_text") and
              r.get("bank_name") and "added" in r for r in ab_runs[:3]),
          "%s 条" % len(ab_runs))
    check("合并运行记录最多保留 30 条", len(ab_runs) <= 30)
    code, res = admin("/api/admin/auto-bank/clear", {})
    check("可清空运行记录", code == 200 and
          ((res.get("status") or {}).get("runs") == []))
    code, res = admin("/api/admin/auto-bank/remove",
                      {"bank_id": junior_bank_id})
    check("删除初中库规则后只剩小学库一条",
          code == 200 and [r["bank_id"] for r in res.get("rules") or []] ==
          [ai_bank_id])
    code, res = admin("/api/admin/auto-bank/run", {"bank_id": junior_bank_id})
    check("规则已删除的题库试跑被明确提示先保存",
          code == 409 and "保存" in (res.get("detail") or ""),
          (res.get("detail") or "")[:40])
    # 自检结束前明确关闭，避免残留一个开启的规则
    admin("/api/admin/auto-bank", {"enabled": False, "bank_id": ai_bank_id})

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

    # ---- 设备答题时段：后台保存/读取/删除 + 答题入口拦截 ----
    # 用不存在的 MAC 且不给它任何放行记录，守护线程不会去动真实路由器
    sch_mac = "02:00:00:00:00:88"
    wday = time.localtime().tm_wday
    blocked_days = ["00:00-24:00"] * 7
    blocked_days[wday] = []                       # 今天整天不可答题
    first_bank_id = ((st.get("banks") or [{}])[0]).get("id", "")
    code, res = admin("/api/admin/bank/assign",
                      {"mac": sch_mac, "bank_id": first_bank_id})
    check("时段测试设备临时归属题库", code == 200, res.get("detail", ""))

    code, res = admin("/api/admin/schedule", {
        "mac": sch_mac,
        "schedule": {"enabled": True, "days": blocked_days},
    })
    check("保存按周时段规则", code == 200 and res.get("ok") is True,
          res.get("detail", ""))
    code, res = admin("/api/admin/schedules")
    sst = (res.get("status") or {}).get(sch_mac) or {}
    check("读取时段规则与当前状态",
          code == 200 and sch_mac in (res.get("schedules") or {}) and
          sst.get("limited") and not sst.get("allowed"),
          sst.get("next_label", ""))
    check("时段状态返回翻转时间供前端自动刷新", bool(sst.get("change_at")))
    code, res = start(0, target=sch_mac)
    check("时段外不能开始答题", code == 403 and
          res.get("reason") == "outside_schedule", res.get("detail", "")[:60])

    code, res = admin("/api/admin/schedule", {
        "mac": sch_mac,
        "schedule": {"enabled": True, "days": ["00:00-24:00"] * 7},
    })
    check("改为全天允许并保存", code == 200 and
          (res.get("status") or {}).get("allowed") is True)
    code, res = start(0, target=sch_mac)
    check("时段内可以开始答题", code == 200, "HTTP %s" % code)

    code, res = admin("/api/admin/schedule", {
        "mac": sch_mac, "schedule": {"enabled": True, "days": ["bad"]},
    })
    check("非法时间段被后台拒绝", code == 400, res.get("detail", "")[:40])
    code, res = admin("/api/admin/schedule",
                      {"mac": sch_mac, "schedule": None})
    check("删除时段规则", code == 200 and res.get("schedule") is None)
    code, res = admin("/api/admin/schedules")
    check("删除后规则不再存在", sch_mac not in (res.get("schedules") or {}))
    code, res = admin("/api/admin/schedule", {"mac": sch_mac})
    check("缺少规则内容时明确报错", code == 400)
    admin("/api/admin/bank/assign", {"mac": sch_mac, "bank_id": ""})

    code, res = admin("/api/admin/logout", {})
    code2, res2 = admin("/api/admin/state")
    check("退出登录后会话失效", code == 200 and code2 == 401)

    # ---- 前端调用约定检查（防止再次把 options 当 data 传给 api()）----
    with open(os.path.join(BASE, "static", "admin.html"), encoding="utf-8") as f:
        admin_html = f.read()
    bad = re.search(r"api\(\s*['\"][^'\"]+['\"]\s*,\s*\{[^{}]*\b(?:method|body)\s*:",
                    admin_html, re.S)
    check("后台前端 api() 调用约定正确", bad is None,
          (bad.group(0)[:50].replace("\n", " ") if bad else ""))
    check("后台设备名旁有倒计时占位", "className = 'cd'" in admin_html and
          "data-mac" in admin_html)
    check("后台倒计时每秒刷新且到期自动更新",
          "setInterval(tickCountdowns, 1000)" in admin_html and
          "setTimeout(refreshDevices, 1500)" in admin_html)
    check("后台有按周时段设置弹窗与接口",
          "openSchedule" in admin_html and "/api/admin/schedule" in admin_html
          and "data-preset" in admin_html)
    check("后台设备页显示可答题/禁止答题状态",
          "sch-tag" in admin_html and "禁止答题" in admin_html and
          "schChangePending" in admin_html)
    with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as f:
        index_html = f.read()
    check("答题页处理时段外提示并到点自动刷新",
          "outside_schedule" in index_html and
          "scheduleAutoRefresh" in index_html and
          "现在不是答题时间" in index_html)

    # ---- device_list_raw 登录态失效自动重登（离线单元测试，不碰真实路由器）----
    real_get, real_post = router_adapter._get, router_adapter._post
    try:
        seq = {"n": 0}

        def fake_get(url, timeout=6):
            seq["n"] += 1
            if seq["n"] == 1:
                # 模拟缓存的 stok 失效：路由器返回 code=401
                return json.dumps({"code": 401, "msg": "token expired"})
            return json.dumps({"list": [{"mac": "02:00:00:00:00:09"}]})

        def fake_post(url, data, timeout=6):
            # 模拟重新登录成功，返回带新 stok 的跳转地址
            return json.dumps({"url": "/cgi-bin/luci/;stok=NEWTOK/api/x"})

        router_adapter._get = fake_get
        router_adapter._post = fake_post
        cfg = {"base_url": "http://router.test",
               "login_path": "/login",
               "device_list_path": "/dl/{stok}",
               "device_list_fallback_path": "",
               "admin_password": "x"}
        ad = router_adapter.MiWifiAdapter(cfg, lambda m: None)
        raw = json.loads(ad.device_list_raw())
        check("设备列表登录态失效自动重登",
              seq["n"] == 2 and bool(raw.get("list")),
              "401 后重登一次，第 %s 次成功" % seq["n"])

        seq["n"] = 0
        ad2 = router_adapter.MiWifiAdapter(dict(cfg, admin_password=""),
                                          lambda m: None)
        ad2._cached_stok = "OLDTOKEN"
        try:
            ad2.device_list_raw()
            check("无法重登时设备列表如实报错", False, "没有抛异常")
        except router_adapter.TokenExpired:
            check("无法重登时设备列表如实报错", True)
        except Exception as exc:  # noqa: BLE001
            check("无法重登时设备列表如实报错", False, repr(exc))
    finally:
        router_adapter._get, router_adapter._post = real_get, real_post

    # ---- AI 出题 / 学情离线单元测试（用临时目录，不碰真实 state、不联网）----
    tmp = tempfile.mkdtemp(prefix="wg-ai-selftest-")
    old_l, old_a = (learning.STATE, learning.PATH), (ai_bank.STATE, ai_bank.CONFIG_PATH)
    old_ab = (auto_bank.STATE, auto_bank.PATH)
    try:
        learning.STATE, learning.PATH = tmp, os.path.join(tmp, "learning.json")
        ai_bank.STATE, ai_bank.CONFIG_PATH = tmp, os.path.join(tmp, "ai.json")
        auto_bank.STATE, auto_bank.PATH = tmp, os.path.join(tmp, "auto_bank.json")

        def ts(y, m, d, h=10):
            return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))

        p = progress.current_progress(ts(2026, 11, 14))
        check("11 月推断为第一学期·期中前后",
              p["term_key"] == "autumn" and p["week"] == 11 and
              p["phase"] == "期中前后", p["label"])
        check("3 月推断为第二学期",
              progress.current_progress(ts(2027, 3, 15))["term_key"] == "spring")
        check("7 月推断为暑假（复习范围）",
              progress.current_progress(ts(2026, 7, 20))["vacation"] == "summer" and
              "复习" in progress.current_progress(ts(2026, 7, 20))["scope"])
        p = progress.current_progress(ts(2026, 11, 14), scope="第 1—3 单元（分数）")
        check("手动教学进度范围优先于自动推断",
              p["manual_scope"] == "第 1—3 单元（分数）" and
              p["scope"] == p["manual_scope"])
        plines = "\n".join(progress.prompt_lines(
            {"stage": "小学", "grade": "五年级", "subject": "数学",
             "textbook": progress.DEFAULT_TEXTBOOK}, p))
        check("进度段落含学期/范围/教材/不超纲",
              all(w in plines for w in ("第一学期", "第 1—3 单元", "教材", "不超纲")))
        pr = ai_bank.build_prompt(
            {"stage": "小学", "grade": "五年级", "subject": "数学", "count": 9},
            ai_bank.split_mix(9))
        check("命题 prompt 含进度/广州/三种题型",
              "【教学进度" in pr and "广东省广州市" in pr and
              all(w in pr for w in ("单选题", "多选题", "判断题")))

        ai_bank.save_config({"api_key": "sk-unit-1234wxyz"})
        check("密钥只回显后四位", ai_bank.public_config()["key_hint"] == "sk-****wxyz")
        check("配置接口不含明文密钥",
              "sk-unit-1234wxyz" not in json.dumps(ai_bank.public_config()))
        ai_bank.save_config({"api_key": "sk-****wxyz"})
        check("打码输入不覆盖已存密钥",
              ai_bank.load_config()["api_key"] == "sk-unit-1234wxyz")
        ai_bank.save_config({"clear_key": True})
        check("清除密钥生效", not ai_bank.load_config()["api_key"])

        gen_qs = ai_bank.offline_generate(
            {"stage": "小学", "grade": "四年级", "subject": "数学", "count": 12})
        clean = store.validate_questions(gen_qs)
        check("离线随机出题 12 道且题干不重复",
              len(gen_qs) == 12 and len({q["text"] for q in gen_qs}) == 12)
        check("生成题保留 topic/source 且通过题库校验",
              len(clean) == 12 and all(c["topic"] and c["source"] for c in clean) and
              all(clean[i]["topic"] == gen_qs[i]["topic"] for i in range(12)))
        mix = ai_bank.split_mix(9)
        check("题型自动分配覆盖三种且总数正确",
              sum(mix.values()) == 9 and all(mix.get(k, 0) > 0 for k in
                                             ("single", "multi", "judge")))
        raw_ai = [
            {"type": "single", "text": "广州塔高约 600 米，600 = ?",
             "options": ["6×10²", "6×10³", "0.6×10³", "60×10"], "answer": "A",
             "explain": "600 = 6×10²。", "topic": "科学记数法"},
            {"type": "multi", "text": "哪些是岭南水果？",
             "options": ["荔枝", "龙眼", "牡丹", "菠萝"], "answer": "A,B,D",
             "explain": "荔枝龙眼菠萝为岭南水果。", "topic": "乡土地理"},
            {"type": "judge", "text": "广州是广东省省会。",
             "options": ["正确", "错误"], "answer": "正确",
             "explain": "广州是省会。", "topic": "乡土地理"},
            {"type": "single", "text": "bad", "options": ["a"], "answer": 0},
        ]
        parsed = ai_bank.normalize_ai_questions(
            raw_ai, {"stage": "初中", "grade": "八年级", "subject": "数学"})
        check("AI 返回解析字母/中文答案并剔除非题",
              len(parsed) == 3 and parsed[0]["answer"] == 0 and
              parsed[1]["answer"] == [0, 1, 3] and parsed[2]["answer"] == 0,
              str([q["answer"] for q in parsed]))
        # 3.8.1：模型常把真换行写进 JSON 字符串，严格解析会整批作废并降级离线
        raw_ctrl = ('[{"type":"single","text":"第一行\n第二行","options":["A","B"],'
                    '"answer":"A","explain":"说明\t带制表符","topic":"分数"}]')
        parsed_ctrl = ai_bank._extract_json(raw_ctrl)
        check("AI 返回带裸换行的 JSON 也能解析",
              len(parsed_ctrl) == 1 and "第二行" in parsed_ctrl[0]["text"],
              json.dumps(parsed_ctrl, ensure_ascii=False)[:80])

        # ---- 3.9.0 出题科目可多选 ----
        check("科目多选解析：顿号/逗号/数组/去重/回落",
              ai_bank.normalize_subjects("数学、语文，英语") == ["数学", "语文", "英语"] and
              ai_bank.normalize_subjects(["数学", "数学", "科学"]) == ["数学", "科学"] and
              ai_bank.normalize_subjects("") == ["数学"] and
              ai_bank.normalize_subjects(["科学", "历史"]) == ["科学", "历史"])
        check("多科目题量平均分配",
              ai_bank.allocate(30, 3) == [10, 10, 10] and
              ai_bank.allocate(10, 3) == [4, 3, 3], str(ai_bank.allocate(10, 3)))
        multi_spec = {"stage": "小学", "grade": "四年级",
                      "subjects": ["数学", "语文", "英语"], "count": 9}
        multi_qs = ai_bank.offline_generate(multi_spec)
        subj_count = {}
        for q in multi_qs:
            subj_count[q["subject"]] = subj_count.get(q["subject"], 0) + 1
        check("离线多科出题：每科都有且总量正好",
              len(multi_qs) == 9 and set(subj_count) == {"数学", "语文", "英语"} and
              all(v == 3 for v in subj_count.values()), str(subj_count))
        multi_prompt = ai_bank.build_prompt(dict(multi_spec), ai_bank.split_mix(9))
        check("多科目 prompt 写明每科题量与 subject 约束",
              "混合以下科目" in multi_prompt and "数学 3 道" in multi_prompt and
              "语文 3 道" in multi_prompt and
              "subject 字段只能写实际所属的那一科" in multi_prompt)
        ai_multi = ai_bank.normalize_ai_questions(
            [{"type": "single", "text": "1+1=?", "options": ["1", "2", "3", "4"],
              "answer": 0, "subject": "数学（四年级）"}],
            {"stage": "小学", "subjects": ["数学", "语文"], "count": 1})
        check("多科目下科目名归一化，数字答案 0 不再被丢",
              len(ai_multi) == 1 and ai_multi[0]["subject"] == "数学" and
              ai_multi[0]["answer"] == 0,
              str([(q["subject"], q["answer"]) for q in ai_multi]))

        M = "02:00:00:00:00:66"
        pool = [{"id": "q%02d" % i, "text": "题%d" % i, "subject": "数学",
                 "type": ["single", "multi", "judge"][i % 3],
                 "topic": "分数" if i < 10 else ("小数" if i < 20 else "加减法"),
                 "options": ["A", "B"], "answer": 0, "explain": ""}
                for i in range(30)]
        for i in range(4):
            learning.record_answer(M, pool[i], False)
        for i in range(10, 15):
            learning.record_answer(M, pool[i], i < 14)
        for i in range(20, 24):
            learning.record_answer(M, pool[i], True)
        ability = learning.ability(M)
        check("学情统计总题数/正确率/错题数",
              ability["total"] == 13 and ability["wrong_count"] == 5 and
              ability["accuracy"] == round(8 / 13, 3),
              "%s 题 / %s 错题 / %s 正确率" %
              (ability["total"], ability["wrong_count"], ability["accuracy"]))
        check("弱项识别（分数掌握度最低）",
              bool(ability["weak"]) and ability["weak"][0]["topic"] == "分数",
              str([w["topic"] for w in ability["weak"]]))
        later = time.time() + 3 * 3600
        check("错题按间隔安排到期复习", learning.ability(M, later)["due_count"] >= 4)
        topic_hits, over_cap = {}, 0
        for _ in range(200):
            picked = learning.pick_questions(pool, M, 6, later)
            c = {}
            for q in picked:
                k = learning.topic_of(q)
                c[k] = c.get(k, 0) + 1
                topic_hits[k] = topic_hits.get(k, 0) + 1
            if c and max(c.values()) > 3:
                over_cap += 1
        check("弱项加权出题但单知识点不占满一轮",
              topic_hits.get("分数", 0) > topic_hits.get("加减法", 0) and
              over_cap == 0, "超上限 %s/200 次" % over_cap)
        one = learning.pick_questions(pool, M, 6, later)
        check("一轮题型混合且不重复",
              len({q["id"] for q in one}) == len(one) and
              len({q["type"] for q in one}) >= 2)

        # ---- 3.13.0 组卷四层优先级：错题 → 新题 → 毕业错题 → 已会题 ----
        check("组卷配额按 40/40/10/10 分配",
              learning._quotas(10, learning.PICK_RATIOS) == [4, 4, 1, 1] and
              learning._quotas(6, learning.PICK_RATIOS) == [2, 2, 1, 1] and
              learning._quotas(1, learning.PICK_RATIOS) == [1, 0, 0, 0] and
              learning._quotas(5, learning.PICK_RATIOS) == [2, 2, 1, 0],
              str(learning._quotas(5, learning.PICK_RATIOS)))
        # 造一题「毕业错题」：错过一次，之后连对两次移出错题本
        for ok in (False, True, True):
            learning.record_answer(M, pool[17], ok)

        def _tier_of(qid):
            """按 M 的学情判断该题属于哪一层（A 错题 / B 新题 / C 毕业 / D 已会）。"""
            if qid in ("q00", "q01", "q02", "q03", "q14"):
                return "A"
            if qid == "q17":
                return "C"
            if qid in ("q10", "q11", "q12", "q13",
                       "q20", "q21", "q22", "q23"):
                return "D"
            return "B"

        mix_ok, buckets = True, {}
        for _i in range(3):          # 乱序出题，多跑几次防偶然
            picked10 = learning.pick_questions(pool, M, 10, later)
            buckets = {}
            for q in picked10:
                k = _tier_of(q["id"])
                buckets[k] = buckets.get(k, 0) + 1
            if buckets != {"A": 4, "B": 4, "C": 1, "D": 1}:
                mix_ok = False
                break
        check("一轮 10 题按 4 错题/4 新题/1 毕业错题/1 已会题组成",
              mix_ok, str(buckets))

        # 错题只有 2 道时：份额顺延，整轮 6 题 = 2 错题 + 4 新题
        # （M2 只做过 q00/q01 两道错题，对它来说其余全是没做过的新题）
        M2 = "02:00:00:00:00:77"
        learning.record_answer(M2, pool[0], False)
        learning.record_answer(M2, pool[1], False)
        p2 = learning.pick_questions(pool, M2, 6, later)
        p2_ids = {q["id"] for q in p2}
        check("某层题不够时份额自动顺延（2 道错题 + 4 道新题）",
              len(p2) == 6 and len(p2_ids) == 6 and
              len(p2_ids & {"q00", "q01"}) == 2 and
              len(p2_ids - {"q00", "q01"}) == 4,
              str(sorted(p2_ids)))
        learning.clear(M2)

        check("清空学情后无残留设备",
              learning.clear(M) and learning.all_devices() == [])
        dev_gen = ai_bank.generate_for_device(
            {"count": 6, "mode": "offline", "subject": "数学"},
            {"weak": [{"topic": "分数"}], "wrong": []})
        check("按设备弱项生成专项题", len(dev_gen["questions"]) == 6)

        # ---- 3.10.0 自动补题：多题库规则、阈值触发与冷却的纯逻辑 ----
        from datetime import datetime
        ab_now = datetime(2026, 11, 14, 11, 0,
                          tzinfo=auto_bank.TZ).timestamp()
        check("时间按广州（UTC+8）显示",
              auto_bank.fmt_ts(ab_now) == "2026-11-14 11:00")
        try:
            auto_bank.normalize({"threshold": 3})
            check("非法触发阈值被拒绝", False)
        except auto_bank.AutoBankError:
            check("非法触发阈值被拒绝", True)
        ab_cfg = auto_bank.normalize({"enabled": True, "threshold": 30,
                                      "cooldown_minutes": 30})
        check("冷却结束时刻 = 本次时刻 + 冷却间隔",
              auto_bank.cooldown_until(ab_cfg, ab_now) - ab_now == 30 * 60)
        check("未启用时不安排冷却",
              auto_bank.cooldown_until(
                  auto_bank.normalize({"enabled": False}), ab_now) == 0)
        ab_multi = auto_bank.normalize({"subjects": ["数学", "英语"]})
        check("自动出题支持科目多选（含兼容字段）",
              ab_multi["subjects"] == ["数学", "英语"] and
              ab_multi["subject"] == "数学、英语", str(ab_multi["subjects"]))
        check("旧配置只有单科目时自动补成列表",
              auto_bank.normalize({"subject": "语文"})["subjects"] == ["语文"] and
              auto_bank.normalize({"subjects": ["语文", "语文"]})["subjects"] == ["语文"])

        # 守护线程的核心路径：两套题库两套规则，各补各的、互不影响。
        # 临时 runtime + 临时学情，不碰真实数据。
        old_rt = store.RUNTIME_PATH
        try:
            store.RUNTIME_PATH = os.path.join(tmp, "runtime.json")
            M2 = "02:00:00:00:00:88"
            seed = [{"id": "f%02d" % i, "type": "single",
                     "text": "原始题%d" % i, "options": ["A", "B"],
                     "answer": 0, "explain": "", "stage": "小学",
                     "subject": "数学", "star": 1} for i in range(20)]
            seed_j = [{"id": "j%02d" % i, "type": "single",
                       "text": "初中题%d" % i, "options": ["A", "B"],
                       "answer": 0, "explain": "", "stage": "初中",
                       "subject": "物理", "star": 2} for i in range(15)]
            with open(store.RUNTIME_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 3,
                    "banks": [
                        {"id": "bank_test", "name": "自动自检题库",
                         "enabled": True, "devices": [],
                         "tiers": [{"questions": 1, "minutes": 30}],
                         "questions": seed},
                        {"id": "bank_junior", "name": "自检初中库",
                         "enabled": True, "devices": [],
                         "tiers": [{"questions": 1, "minutes": 30}],
                         "questions": seed_j},
                    ],
                    "settings": {}, "schedules": {},
                }, f, ensure_ascii=False)
            fake_st = store.Store()

            # 3.12.0：放行规则存在题库上，两套题库各配各的
            fake_st.save_bank({"id": "bank_test", "name": "自动自检题库",
                               "tiers": [{"questions": 1, "minutes": 30}],
                               "devices": [], "questions": seed,
                               "pass_mode": "strict", "pass_rate_percent": 90,
                               "wrong_add_questions": 0})
            check("题库 A 保存自己的放行规则（严格 / 90% / 不加题）",
                  fake_st.bank("bank_test")["pass_mode"] == "strict" and
                  fake_st.bank("bank_test")["pass_rate_percent"] == 90 and
                  fake_st.bank("bank_test")["wrong_add_questions"] == 0)
            check("题库 B 不受题库 A 的规则影响（仍为默认 80% / 加 2 题）",
                  fake_st.bank("bank_junior")["pass_mode"] == "rate" and
                  fake_st.bank("bank_junior")["pass_rate_percent"] == 80 and
                  fake_st.bank("bank_junior")["wrong_add_questions"] == 2,
                  str(fake_st.bank("bank_junior").get("pass_rate_percent")))

            # 旧版（3.9.0 及以前）单份配置文件 → 首次读取自动迁移成一条规则
            with open(auto_bank.PATH, "w", encoding="utf-8") as f:
                json.dump({"enabled": True, "threshold": 25,
                           "cooldown_minutes": 20, "subjects": ["语文"]}, f)
            migrated = auto_bank.load_all(fake_st)
            check("旧版单份配置自动迁移为规则列表并解析到第一个题库",
                  len(migrated) == 1 and migrated[0]["threshold"] == 25 and
                  migrated[0]["subjects"] == ["语文"] and
                  migrated[0]["bank_id"] == "bank_test")

            auto_bank.save_rule({
                "enabled": True, "threshold": 30, "cooldown_minutes": 30,
                "count": 30, "mode": "offline", "max_auto": 100,
                "bank_id": "bank_test", "stage": "小学", "grade": "四年级",
                "subjects": ["数学"],
            }, fake_st, ab_now)
            auto_bank.save_rule({
                "enabled": True, "threshold": 8, "cooldown_minutes": 60,
                "count": 5, "mode": "offline", "max_auto": 50,
                "bank_id": "bank_junior", "stage": "初中", "grade": "八年级",
                "subjects": ["物理", "英语"],
            }, fake_st, ab_now)
            rules = {r["bank_id"]: r for r in auto_bank.load_all(fake_st)}
            check("两个题库各存一条独立规则",
                  len(rules) == 2 and rules["bank_test"]["threshold"] == 30 and
                  rules["bank_junior"]["threshold"] == 8 and
                  rules["bank_junior"]["subjects"] == ["物理", "英语"],
                  str({k: r["threshold"] for k, r in rules.items()}))

            rule1 = auto_bank.load_rule(fake_st, "bank_test")
            check("没人答过题时整库都是新题",
                  auto_bank.fresh_count(fake_st, rule1) == 20)
            check("保存设置后先进入冷却，缺题也不会立刻触发",
                  auto_bank.fresh_count(fake_st, rule1) < rule1["threshold"]
                  and not auto_bank.due(fake_st, rule1, ab_now))
            for q in seed[:18]:            # 孩子答掉 18 道，只剩 2 道新题
                learning.record_answer(M2, q, True)
            check("答过的题不再算新题",
                  auto_bank.fresh_count(fake_st, rule1) == 2)
            rule2 = auto_bank.load_rule(fake_st, "bank_junior")
            check("初中库新题充足（阈值 8）不触发，与小学库互不影响",
                  not auto_bank.due(fake_st, rule2, ab_now))

            rule1["next_run"] = ab_now - 1        # 冷却已结束
            auto_bank._save_doc({"version": 2,
                                 "rules": [rule1, rules["bank_junior"]]})
            rule1 = auto_bank.load_rule(fake_st, "bank_test")
            check("新题低于阈值且冷却结束，对守护线程可见",
                  auto_bank.due(fake_st, rule1, ab_now))
            rec = auto_bank.run_once(fake_st, rule1, "auto", ab_now)
            check("守护线程自动补题入库，全程无需人工操作",
                  rec["ok"] and rec["reason"] == "auto" and rec["added"] >= 1,
                  "生成 %s，入库 %s" % (rec["generated"], rec["added"]))
            check("只补缺口：缺 28 道就生成 28 道",
                  rec["generated"] == 28, "生成 %s 道" % rec["generated"])
            after = auto_bank.load_rule(fake_st, "bank_test")
            check("执行后进入冷却并留下自动触发记录",
                  after["next_run"] - ab_now == 30 * 60 and
                  bool(after["runs"]) and
                  after["runs"][0]["reason"] == "auto")
            check("补完后新题回到不低于阈值",
                  auto_bank.fresh_count(fake_st, after) >= 30,
                  "新题 %s 道" % auto_bank.fresh_count(fake_st, after))
            check("冷却期内即使缺题也不触发",
                  not auto_bank.due(fake_st, after, ab_now + 60))
            check("冷却结束但新题充足也不触发",
                  not auto_bank.due(fake_st, after, ab_now + 3600))
            check("题库里出现自动来源的新题",
                  any(str(q.get("source") or "").startswith("auto-")
                      for q in fake_st.bank("bank_test")["questions"]))
            check("只写目标题库，另一个题库纹丝不动",
                  len(fake_st.bank("bank_junior")["questions"]) == 15)
            ab_status = auto_bank.status(fake_st, ab_now)
            check("状态卡按题库分行，两库阈值各是各的",
                  {b["bank_id"]: b["threshold"]
                   for b in ab_status["banks"]} ==
                  {"bank_test": 30, "bank_junior": 8})
            check("运行记录合并展示且带题库名",
                  len(ab_status["runs"]) >= 1 and
                  bool(ab_status["runs"][0]["bank_name"]))
            for q in seed[18:]:           # 又答掉剩下的 2 道原始题
                learning.record_answer(M2, q, True)
            check("缺题但冷却未结束，仍不触发",
                  auto_bank.fresh_count(fake_st, after) < after["threshold"]
                  and not auto_bank.due(fake_st, after, ab_now + 60))
            later2 = ab_now + 31 * 60
            check("冷却结束后缺题状态会再次触发",
                  auto_bank.due(
                      fake_st, auto_bank.load_rule(fake_st, "bank_test"), later2))

            # 初中库也走一遍：答空 → 冷却结束 → 按它自己的科目出题
            for q in seed_j:
                learning.record_answer(M2, q, True)
            jr = auto_bank.load_rule(fake_st, "bank_junior")
            check("初中库答空后低于它自己的阈值，但仍在冷却期内",
                  auto_bank.fresh_count(fake_st, jr) == 0 and
                  not auto_bank.due(fake_st, jr, ab_now))
            jr["next_run"] = ab_now - 1
            auto_bank._save_doc({"version": 2, "rules":
                                 [auto_bank.load_rule(fake_st, "bank_test"), jr]})
            jr = auto_bank.load_rule(fake_st, "bank_junior")
            check("初中库冷却结束后也会触发",
                  auto_bank.due(fake_st, jr, ab_now))
            rec2 = auto_bank.run_once(fake_st, jr, "auto", ab_now)
            junior_added = [q for q in fake_st.bank("bank_junior")["questions"]
                            if str(q.get("source") or "").startswith("auto-")]
            check("初中库按它自己的规则补题（科目对、量受它自己的上限约束）",
                  rec2["ok"] and rec2["generated"] == 5 and
                  len(junior_added) == 5 and
                  all(q.get("subject") in ("物理", "英语") for q in junior_added),
                  "生成 %s 道" % rec2["generated"])
            check("初中库执行后进入它自己的冷却（60 分钟）",
                  auto_bank.load_rule(fake_st, "bank_junior")["next_run"] -
                  ab_now == 60 * 60)

            # 3.12.0：老数据（3.11.0 及以前）题库里没有放行规则 →
            # 继承当时的全局设置，升级后判卷行为与升级前完全一致。
            with open(store.RUNTIME_PATH, "w", encoding="utf-8") as f:
                json.dump({"version": 3, "banks": [
                    {"id": "bank_old", "name": "老题库", "enabled": True,
                     "devices": [], "tiers": [{"questions": 1, "minutes": 30}],
                     "questions": seed}],
                    "settings": {"pass_mode": "strict", "pass_rate_percent": 70,
                                 "wrong_add_questions": 5},
                    "schedules": {}}, f, ensure_ascii=False)
            legacy_st = store.Store()
            lb = legacy_st.bank("bank_old")
            check("老题库自动继承全局放行规则（升级不改行为）",
                  lb["pass_mode"] == "strict" and
                  lb["pass_rate_percent"] == 70 and
                  lb["wrong_add_questions"] == 5,
                  "%s / %s%% / %s" % (lb["pass_mode"],
                                      lb["pass_rate_percent"],
                                      lb["wrong_add_questions"]))
        finally:
            store.RUNTIME_PATH = old_rt
    finally:
        learning.STATE, learning.PATH = old_l
        ai_bank.STATE, ai_bank.CONFIG_PATH = old_a
        auto_bank.STATE, auto_bank.PATH = old_ab
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 3.6.0 前端静态检查 ----
    check("后台提供 API Key 密码框并说明只存 state/ai.json",
          'id="ai-key" type="password"' in admin_html and
          "state/ai.json" in admin_html and "sk-****" in admin_html)
    check("后台有 AI 出题与学情两个页签",
          all(w in admin_html for w in
              ("tabbtn-ai", "tab-learning", "ai-progress", "ai-preview",
               "learn-cards", "learn-detail")))
    check("后台调用全部 AI/学情接口",
          all(("/api/admin/ai/" + w) in admin_html for w in
              ("config", "test", "generate", "apply")) and
          "/api/admin/learning" in admin_html)
    check("后台可手动覆盖教学进度", "ai-prog-auto" in admin_html and
          "ai-scope" in admin_html and "ai-textbook" in admin_html)
    check("答题页显示知识点与本轮重点", "q.topic" in index_html and
          "本轮重点练习" in index_html)

    # ---- 3.6.1：AI 预览题目必须可见 ----
    # 题库列表用 .qbody{display:none} 做折叠；预览卡片若沿用同名类且未覆盖，
    # 题目正文会整块被隐藏（只剩勾选框）。两条规则缺一不可。
    check("题库折叠样式限定在 .qcard 内",
          bool(re.search(r"\.qcard\s*>\s*\.qbody\s*\{[^}]*display\s*:\s*none", admin_html)) and
          ".qcard.open > .qbody" in admin_html)
    check("AI 预览题目正文强制可见",
          bool(re.search(r"\.qprev\s+\.qbody\s*\{[^}]*display\s*:\s*block", admin_html)))

    # ---- 3.10.0 自动补题（每库一套规则的阈值触发）----
    with open(os.path.join(BASE, "server.py"), encoding="utf-8") as f:
        server_html = f.read()
    with open(os.path.join(BASE, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    with open(os.path.join(BASE, "learning.py"), encoding="utf-8") as f:
        learning_src = f.read()
    check("组卷按四层优先级取题（错题/新题/毕业错题/已会题）",
          "PICK_RATIOS = (0.40, 0.40, 0.10, 0.10)" in learning_src and
          "def _quotas" in learning_src and
          "shortfall" in learning_src and
          "_weak_order" in learning_src and
          "四层优先级" in server_html)
    check("组卷仍保留弱项加权与单知识点上限",
          "_weighted_pick" in learning_src and
          "topic_cap" in learning_src and
          "_enforce_topic_cap" in learning_src)
    check("服务端守护线程逐库检查、逐库补题",
          "def auto_bank_worker" in server_html and
          "target=auto_bank_worker" in server_html and
          "for rule in auto_bank.load_all(STORE)" in server_html and
          "auto_bank.due(STORE, rule)" in server_html and
          'run_once(STORE, rule, "auto")' in server_html)
    check("新模块已纳入镜像", "auto_bank.py" in dockerfile)
    check("后台有「自动出题」页签与阈值/冷却/按库设置项",
          all(w in admin_html for w in
              ("tabbtn-auto", "tab-auto", "auto-enabled", "auto-threshold",
               "auto-cooldown", "auto-count", "auto-bank",
               "auto-mode", "auto-max", "auto-run", "auto-runs",
               "auto-remove")) and
          "auto-frequency" not in admin_html and "auto-time" not in admin_html)
    check("后台调用自动出题配置/试跑/删除/清空接口",
          all(w in admin_html for w in
              ("/api/admin/auto-bank/run", "/api/admin/auto-bank/clear",
               "/api/admin/auto-bank/remove",
               "saveAutoBank", "runAutoBank", "removeAutoRule",
               "refreshAutoStatus")))
    ab_refresh = re.search(r"function refreshAutoStatus\(\)[\s\S]*?\n  }",
                           admin_html)
    check("自动出题定时刷状态不覆盖正在编辑的表单",
          bool(ab_refresh) and "renderAutoBank" not in ab_refresh.group(0) and
          "renderAutoStatus" in ab_refresh.group(0))
    check("题库下拉切换时重新装填该库的表单",
          "renderAutoRuleForm" in admin_html and
          "el('auto-bank').onchange" in admin_html and
          "bank_id: currentAutoBank()" in admin_html)
    check("状态卡按题库分行汇总（每库一行）",
          "<th>题库</th><th>自动出题</th>" in admin_html and
          "b.configured" in admin_html and
          "该题库" in admin_html)

    # ---- 3.9.0 出题科目多选 ----
    check("科目改成多选控件（不再是单选下拉）",
          bool(re.search(r'<div id="ai-subject"[^>]*class="chips"', admin_html)) and
          bool(re.search(r'<div id="auto-subject"[^>]*class="chips"', admin_html)) and
          '<select id="ai-subject"' not in admin_html and
          '<select id="auto-subject"' not in admin_html)
    check("前端按勾选框读写科目数组",
          "function subjectChips(" in admin_html and
          "function subjectPick(" in admin_html and
          "subjects: subs" in admin_html and
          "box.querySelectorAll('input')" in admin_html)
    check("至少勾一个科目才允许出题/保存",
          admin_html.count("请至少勾选一个科目") >= 2,
          str(admin_html.count("请至少勾选一个科目")))

    # ---- 3.12.0 放行规则：按题库配置（答错加题 + 正确率达标）----
    check("放行规则三条配置放在「题库」页签，不再是全局设置",
          "放行规则<span class=\"count\">只对本题库生效</span>" in admin_html and
          all(w in admin_html for w in
              ('id="bank-pass-mode"', 'id="bank-pass-rate"',
               'id="bank-wrong-add"',
               "pass_mode: el('bank-pass-mode').value",
               "pass_rate_percent: Number(el('bank-pass-rate').value)",
               "wrong_add_questions: Number(el('bank-wrong-add').value)")) and
          'id="pass-mode"' not in admin_html and
          'id="wrong-add"' not in admin_html)
    check("切换题库时回填该题库自己的放行规则",
          "el('bank-pass-mode').value = b.pass_mode" in admin_html and
          "el('bank-pass-rate').value = b.pass_rate_percent" in admin_html and
          "el('bank-wrong-add').value = b.wrong_add_questions" in admin_html)
    check("判卷支持两种模式且默认正确率放行",
          'value="rate">正确率达到阈值就放行' in admin_html and
          'value="strict">一轮全部答对才放行' in admin_html)
    check("服务端按题库规则判卷（开答时把规则快照进本轮）",
          '"pass_rule": pass_rule(bank)' in server_html and
          'sess.get("pass_rule")' in server_html and
          "added_questions" in server_html and
          "accuracy_percent" in server_html and
          "correct_n * 100 >= rate * total" in server_html)
    check("答错加题只挑本轮没出过的题",
          "sess[\"bank_id\"]" in server_html and
          "x[\"id\"] not in used" in server_html)
    check("答题页展示加题提示与正确率结算",
          "本轮加做" in index_html and "正确率" in index_html and
          "本轮正确率" in index_html and
          "passRuleText" in index_html and
          "if (d.total) S.total = d.total" in index_html)
    check("答题页按所选设备的题库显示放行规则",
          "function ruleOf(t)" in index_html and
          "(t && t.pass_rule)" in index_html and
          "passRuleText(t)" in index_html and
          "即可放行" in index_html)

    # ---- 3.14.0 限制方式：限速（不切断外网，答对解除）----
    lr = store.validate_limit_rule({})
    check("限制方式默认断网 + 限速默认 50KB/s",
          lr == {"limit_mode": "block", "throttle_kbps": 50}, str(lr))

    def _raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
        except store.StoreError:
            return True
        except Exception:
            return True
        return False

    check("限制方式非法值会被拒绝",
          _raises(store.validate_limit_rule, {"limit_mode": "bogus"}) and
          _raises(store.validate_limit_rule, {"throttle_kbps": 0}) and
          _raises(store.validate_limit_rule, {"throttle_kbps": "abc"}))
    check("题库校验带出限制方式字段",
          store.validate_bank(dict(TEST_BANK, limit_mode="throttle",
                                   throttle_kbps=250)).get("limit_mode")
          == "throttle")

    mock_ad = router_adapter.MockAdapter({}, lambda m: None)
    mock_ad.set_limit("AA:BB:CC:DD:EE:FF", 250, 250)
    check("mock 适配器记录限速",
          mock_ad.limit_of("aa:bb:cc:dd:ee:ff") == {"down": 250, "up": 250})
    mock_ad.set_limit("AA:BB:CC:DD:EE:FF", 0, 0)
    check("mock 适配器解除限速",
          mock_ad.limit_of("aa:bb:cc:dd:ee:ff") == {"down": 0, "up": 0})

    # 换算函数（3.14.1：路由器接口单位是 Mbps 整数，对外统一 KB/s）
    conv = router_adapter.kbps_to_router_mbps
    back = router_adapter.router_mbps_to_kbps
    check("限速值换算 KB/s→Mbps（250→2、50→1、0→0、小数被拒绝）",
          conv(250) == 2 and conv(50) == 1 and conv(0) == 0 and
          conv(125) == 1 and conv(1000) == 8 and conv(-5) == 0 and
          conv("abc") == 0, str((conv(250), conv(50), conv(125), conv(1000))))
    check("限速值回读 Mbps→KB/s（2→250、0→0、非法→0）",
          back(2) == 250 and back(0) == 0 and back("x") == 0 and back(None) == 0)
    check("换算往返一致（250→2→250）",
          back(conv(250)) == 250 and back(conv(125)) == 125)

    _lim_mac = "aa:bb:cc:dd:ee:01"
    _thr_bank = {"limit_mode": "throttle", "throttle_kbps": 250}
    # 接线用例用 Mock 跑：真适配器按假 MAC 下发后 qos_info 里查不到这台
    # 「设备」（不在路由器设备列表），limit_of 恒为 0，用例会误报。
    _saved_adapter = srv.ADAPTER
    srv.ADAPTER = router_adapter.MockAdapter({}, lambda m: None)
    mode = srv.restrict_mac(_lim_mac, _thr_bank, "自检")
    check("到点按限制方式改成限速（不断网）",
          mode == "throttle" and
          srv.ADAPTER.limit_of(_lim_mac)["down"] == 250)
    srv.release_mac(_lim_mac, _thr_bank)
    check("答对后解除限速",
          srv.ADAPTER.limit_of(_lim_mac)["down"] == 0)
    mode = srv.restrict_mac(_lim_mac, {"limit_mode": "block"}, "自检")
    check("断网模式仍然走 set_wan 断开",
          mode == "block" and srv.ADAPTER.limit_of(_lim_mac)["down"] == 0)
    srv.release_mac(_lim_mac, {"limit_mode": "block"})
    check("断网模式放行也顺手清残留限速",
          srv.ADAPTER.limit_of(_lim_mac)["down"] == 0)
    srv.ADAPTER = _saved_adapter

    # 前面的「退出登录」用例已经注销过，这里重新登录再做后台接口检查
    admin("/api/admin/login", {"password": CFG["server"].get("admin_password", "")})
    code, saved = admin("/api/admin/bank", {
        "name": "限制方式自检库",
        "tiers": [{"questions": 1, "minutes": 10}],
        "devices": [],
        "questions": [TEST_BANK["questions"][0]],
        "pass_mode": "strict",
        "pass_rate_percent": 80,
        "wrong_add_questions": 0,
        "limit_mode": "throttle",
        "throttle_kbps": 250,
    })
    ok_bank = (code == 200 and saved.get("bank", {}).get("limit_mode") == "throttle"
               and saved.get("bank", {}).get("throttle_kbps") == 250)
    check("后台保存「限制方式=限速」并回读", ok_bank,
          str(saved.get("detail", ""))[:40])
    code, bad = admin("/api/admin/bank", dict(
        TEST_BANK, limit_mode="throttle", throttle_kbps=0))
    check("非法限速值被后台拒绝", code == 400, (bad.get("detail") or "")[:30])
    if ok_bank:
        admin("/api/admin/bank/delete", {"id": saved["bank"]["id"]})

    check("后台题库页有「限制方式」配置",
          'id="bank-limit-mode"' in admin_html and
          'id="bank-throttle"' in admin_html and
          "limit_mode: el('bank-limit-mode').value" in admin_html and
          "throttle_kbps: Number(el('bank-throttle').value)" in admin_html and
          "el('bank-limit-mode').value = b.limit_mode" in admin_html)
    check("后台设备页按限制方式显示限速/解除限速",
          "function deviceLimit(" in admin_html and
          "/api/admin/device/limit" in admin_html and
          "bOff.textContent = lm ? '限速' : '断网'" in admin_html and
          "limit_mode === 'throttle'" in admin_html)
    check("答题页提示限速模式",
          "function limitText(t)" in index_html and
          "限速模式" in index_html and "解除限速" in index_html)
    check("服务端把限制方式接进放行/回收链路",
          "def restrict_mac(" in server_html and
          "def release_mac(" in server_html and
          "release_mac(mac, STORE.bank(bank_id)" in server_html and
          "restrict_mac(mac, STORE.bank_for_mac(mac)" in server_html and
          'ADAPTER.set_limit(mac, rule["kbps"], rule["kbps"])' in server_html)
    check("服务端问卷下发限制方式",
          '"limit_rule": limit_rule()' in server_html and
          '"limit_rule": rule,' in server_html and
          '"limit_rule": limit_rule({"limit_mode": t.get("limit_mode")' in server_html and
          "ADAPTER.limit_of(mac)" in server_html)
    with open(os.path.join(BASE, "router_adapter.py"), encoding="utf-8") as f:
        adapter_src = f.read()
    check("限速接口按 Mbps 换算且先确保 QoS 开启（3.14.1 修复）",
          "def kbps_to_router_mbps" in adapter_src and
          "def router_mbps_to_kbps" in adapter_src and
          "self.ensure_qos()" in adapter_src and
          "kbps_to_router_mbps(down)" in adapter_src and
          "QOS_BAND_PATH" in adapter_src and
          "已补设总带宽" in adapter_src,
          "换算/ensure_qos/band 三处缺一不可")
    check("手动解除限速不看题库模式直接清零（3.14.1 修复）",
          "清残留限速" in server_html and
          "按模式走会漏清" in server_html)

    for _p, _raw in extra_paths.items():
        try:
            if _raw is None:
                if os.path.exists(_p):
                    os.remove(_p)
            else:
                with open(_p, "wb") as _f:
                    _f.write(_raw)
        except OSError:
            pass

    print("\n结果：%s 项通过，%s 项失败" % (passed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
