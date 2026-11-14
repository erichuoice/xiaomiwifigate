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

    # ---- 3.8.0 自动补题接口（全程离线，不联网、不消耗 Key）----
    code, res = admin("/api/admin/auto-bank")
    ab_status = res.get("status") or {}
    check("自动出题接口返回配置/状态/题库/选项",
          code == 200 and res.get("ok") is True and
          all(k in res for k in ("config", "status", "banks", "options")) and
          bool((res.get("options") or {}).get("stages")))
    check("状态卡含未做过的题数/阈值/冷却与自动题统计",
          all(k in ab_status for k in
              ("fresh", "threshold", "need", "cooldown_minutes",
               "last_run_text", "auto_questions", "has_key")))
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
        "subject": "数学", "topics": "",
        "mix": {"single": 1, "multi": 1, "judge": 1},
    })
    check("保存自动出题设置（保持关闭，避免自检期间被定时触发）",
          code == 200 and (res.get("config") or {}).get("count") == 3 and
          (res.get("config") or {}).get("max_auto") == 5 and
          (res.get("config") or {}).get("enabled") is False)

    added_total, run_ok = 0, True
    for _i in range(3):
        code, res = admin("/api/admin/auto-bank/run", {})
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
    ab_runs = (res.get("config") or {}).get("runs") or []
    check("每次执行都留下可追溯的运行记录",
          len(ab_runs) >= 3 and
          all(r.get("reason") == "manual" and r.get("at_text") and
              "added" in r for r in ab_runs[:3]), "%s 条" % len(ab_runs))
    check("运行记录最多保留 20 条", len(ab_runs) <= 20)
    code, res = admin("/api/admin/auto-bank/clear", {})
    check("可清空运行记录", code == 200 and
          (res.get("config") or {}).get("runs") == [])
    # 自检结束前明确关闭，避免残留一个开启的定时任务
    admin("/api/admin/auto-bank", {"enabled": False})

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
        check("清空学情后无残留设备",
              learning.clear(M) and learning.all_devices() == [])
        dev_gen = ai_bank.generate_for_device(
            {"count": 6, "mode": "offline", "subject": "数学"},
            {"weak": [{"topic": "分数"}], "wrong": []})
        check("按设备弱项生成专项题", len(dev_gen["questions"]) == 6)

        # ---- 3.8.0 自动补题：阈值触发与冷却的纯逻辑 ----
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

        # 守护线程的核心路径：新题少于阈值 → due → run_once(auto) 补缺口
        # → 进入冷却并留痕。临时 runtime + 临时学情，不碰真实数据。
        old_rt = store.RUNTIME_PATH
        try:
            store.RUNTIME_PATH = os.path.join(tmp, "runtime.json")
            M2 = "02:00:00:00:00:88"
            seed = [{"id": "f%02d" % i, "type": "single",
                     "text": "原始题%d" % i, "options": ["A", "B"],
                     "answer": 0, "explain": "", "stage": "小学",
                     "subject": "数学", "star": 1} for i in range(20)]
            with open(store.RUNTIME_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 3,
                    "banks": [{"id": "bank_test", "name": "自动自检题库",
                               "enabled": True, "devices": [],
                               "tiers": [{"questions": 1, "minutes": 30}],
                               "questions": seed}],
                    "settings": {}, "schedules": {},
                }, f, ensure_ascii=False)
            fake_st = store.Store()
            auto_bank.save({
                "enabled": True, "threshold": 30, "cooldown_minutes": 30,
                "count": 30, "mode": "offline", "max_auto": 100,
                "bank_id": "bank_test", "stage": "小学", "grade": "四年级",
                "subject": "数学",
            }, ab_now)
            ab_cfg = auto_bank.load()
            check("没人答过题时整库都是新题",
                  auto_bank.fresh_count(fake_st, ab_cfg) == 20)
            check("保存设置后先进入冷却，缺题也不会立刻触发",
                  auto_bank.fresh_count(fake_st, ab_cfg) < ab_cfg["threshold"]
                  and not auto_bank.due(fake_st, ab_cfg, ab_now))
            for q in seed[:18]:            # 孩子答掉 18 道，只剩 2 道新题
                learning.record_answer(M2, q, True)
            check("答过的题不再算新题",
                  auto_bank.fresh_count(fake_st, ab_cfg) == 2)
            ab_cfg["next_run"] = ab_now - 1        # 冷却已结束
            auto_bank._save_cfg(ab_cfg)
            check("新题低于阈值且冷却结束，对守护线程可见",
                  auto_bank.due(fake_st, auto_bank.load(), ab_now))
            rec = auto_bank.run_once(fake_st, "auto", ab_now)
            check("守护线程自动补题入库，全程无需人工操作",
                  rec["ok"] and rec["reason"] == "auto" and rec["added"] >= 1,
                  "生成 %s，入库 %s" % (rec["generated"], rec["added"]))
            check("只补缺口：缺 28 道就生成 28 道",
                  rec["generated"] == 28, "生成 %s 道" % rec["generated"])
            ab_after = auto_bank.load()
            check("执行后进入冷却并留下自动触发记录",
                  ab_after["next_run"] - ab_now == 30 * 60 and
                  bool(ab_after["runs"]) and
                  ab_after["runs"][0]["reason"] == "auto")
            check("补完后新题回到不低于阈值",
                  auto_bank.fresh_count(fake_st, ab_after) >= 30,
                  "新题 %s 道" % auto_bank.fresh_count(fake_st, ab_after))
            check("冷却期内即使缺题也不触发",
                  not auto_bank.due(fake_st, ab_after, ab_now + 60))
            check("冷却结束但新题充足也不触发",
                  not auto_bank.due(fake_st, ab_after, ab_now + 3600))
            check("题库里出现自动来源的新题",
                  any(str(q.get("source") or "").startswith("auto-")
                      for q in fake_st.bank("bank_test")["questions"]))
            for q in seed[18:]:           # 又答掉剩下的 2 道原始题
                learning.record_answer(M2, q, True)
            check("缺题但冷却未结束，仍不触发",
                  auto_bank.fresh_count(fake_st, auto_bank.load()) <
                  auto_bank.load()["threshold"] and
                  not auto_bank.due(fake_st, auto_bank.load(), ab_now + 60))
            later2 = ab_now + 31 * 60
            check("冷却结束后缺题状态会再次触发",
                  auto_bank.due(fake_st, auto_bank.load(), later2))
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

    # ---- 3.8.0 自动补题（阈值触发）----
    with open(os.path.join(BASE, "server.py"), encoding="utf-8") as f:
        server_html = f.read()
    with open(os.path.join(BASE, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    check("服务端守护线程按阈值触发自动补题",
          "def auto_bank_worker" in server_html and
          "target=auto_bank_worker" in server_html and
          "auto_bank.due(STORE" in server_html and
          'run_once(STORE, "auto")' in server_html)
    check("新模块已纳入镜像", "auto_bank.py" in dockerfile)
    check("后台有「自动出题」页签与阈值/冷却设置项",
          all(w in admin_html for w in
              ("tabbtn-auto", "tab-auto", "auto-enabled", "auto-threshold",
               "auto-cooldown", "auto-count", "auto-bank",
               "auto-mode", "auto-max", "auto-run", "auto-runs")) and
          "auto-frequency" not in admin_html and "auto-time" not in admin_html)
    check("后台调用自动出题配置/试跑/清空接口",
          all(w in admin_html for w in
              ("/api/admin/auto-bank/run", "/api/admin/auto-bank/clear",
               "saveAutoBank", "runAutoBank", "refreshAutoStatus")))
    ab_refresh = re.search(r"function refreshAutoStatus\(\)[\s\S]*?\n  }",
                           admin_html)
    check("自动出题定时刷状态不覆盖正在编辑的表单",
          bool(ab_refresh) and "renderAutoBank" not in ab_refresh.group(0) and
          "renderAutoStatus" in ab_refresh.group(0))

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
