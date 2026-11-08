#!/usr/bin/env python3
"""模拟一台手机，把答题流程完整走一遍（用于「本机当 NAS」实测）。

    python3 simulate_device.py --url http://192.168.31.41:8080
    python3 simulate_device.py --url http://192.168.31.41:8080 --password 管理密码 --mode correct
    python3 simulate_device.py --url http://192.168.31.41:8080 --password 管理密码 --tier 1 --mode wrong

它就像浏览器一样直接访问 /api/quiz、/api/start、/api/answer，不带
X-Forwarded-For，所以服务端看到的是本机真实内网 IP——正是手机连上 Wi-Fi 后的情形。
"""

import argparse
import http.cookiejar
import json
import urllib.error
import urllib.request

LETTERS = "ABCDEFGH"


def _open(req, opener):
    if opener is not None:
        return opener.open(req, timeout=8)
    return urllib.request.urlopen(req, timeout=8)


def post(url, data, opener=None):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with _open(req, opener) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}


def get(url, opener=None):
    try:
        with _open(url, opener) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}


def answers_of(base, password):
    """以管理员身份读题库答案（模拟设备自己并不知道答案）。"""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    code, _ = post(base + "/api/admin/login", {"password": password}, op)
    if code != 200:
        raise SystemExit("管理员登录失败，无法自动作答（HTTP %s）" % code)
    code, st = get(base + "/api/admin/state", op)
    out = {}
    for bank in st.get("banks") or []:
        for q in bank["questions"]:
            out[q["id"]] = q["answer"]
    return out


def show(q, index, total):
    print("  第 %s/%s 题｜%s %s｜难度 %s"
          % (index + 1, total, q.get("stage") or "-", q.get("subject") or "-",
             q.get("star") or "-"))
    print("  " + q["text"])
    for i, opt in enumerate(q["options"]):
        print("    %s. %s" % (LETTERS[i], opt))


def describe_tiers(tiers):
    return " / ".join(
        "%s 题 → %s" % (t["questions"], "一直放行" if t["minutes"] == 0
                        else "%s 分钟" % t["minutes"]) for t in tiers)


def run(base, password, mode, rounds, tier):
    right_of = answers_of(base, password) if (password and mode != "peek") else {}
    for r in range(rounds):
        code, info = get(base + "/api/quiz")
        if not info.get("ok"):
            print("取题失败：%s" % (info.get("detail") or info))
            return
        if info.get("locked_for"):
            print("本轮被锁定：还需等待 %s 秒" % info["locked_for"])
            return
        if not info.get("need"):
            print("这台设备（%s）不用答题：%s"
                  % (info.get("mac"), info.get("message") or info.get("reason")))
            print("提示：在管理后台「设备」页把本机分配给某个题库后再试。")
            return
        left = info.get("remaining_minutes")
        print("\n=== 第 %s 轮 ===" % (r + 1))
        print("  设备 MAC %s｜题库「%s」" % (info.get("mac"), info["bank"]["name"]))
        print("  可选档位：%s" % describe_tiers(info["tiers"]))
        print("  当前剩余：%s" % ("永久" if left is None else "%s 分钟" % left))
        if tier >= len(info["tiers"]):
            print("  档位索引 %s 超出范围，改用第 0 档" % tier)
            tier = 0
        code, d = post(base + "/api/start", {"tier": tier})
        if code != 200 or not d.get("ok"):
            print("  开始答题失败：%s %s" % (code, d.get("reason") or d.get("detail")))
            return
        print("  本轮：%s 题，答对可得 %s 分钟" % (d["total"], d["minutes"]))
        while True:
            q = d.get("question")
            if not q:
                break
            show(q, d["index"], d["total"])
            if mode == "peek" or not right_of:
                return
            right = right_of.get(q["id"], 0)
            if mode == "correct":
                choices = right if isinstance(right, list) else [right]
            else:
                pool = right if isinstance(right, list) else [right]
                choices = [i for i in range(len(q["options"])) if i not in pool][:1]
            code, d = post(base + "/api/answer", {"id": q["id"], "choices": choices})
            if code in (429, 409) or not d.get("ok"):
                print("  服务返回：%s %s" % (code, d.get("reason") or d.get("detail")))
                return
            mark = "✓ 答对" if d.get("correct") else "✗ 答错"
            print("  %s；正确答案 %s；%s"
                  % (mark,
                     "".join(LETTERS[i] for i in (d["answer"] if isinstance(d["answer"], list)
                                                  else [d["answer"]])),
                     d.get("explain") or "（无解析）"))
            if d.get("done"):
                if d.get("all_correct"):
                    print("  >>> 全部答对：%s（MAC %s）"
                          % (d.get("message"), d.get("mac")))
                    print("  >>> 累计可用 %s 分钟，今日已得 %s 分钟"
                          % (d.get("remaining_minutes"), d.get("today_minutes")))
                else:
                    print("  >>> 这一轮有错题，需要重来；剩余机会 %s 次"
                          % d.get("remain"))
                    if d.get("locked_for"):
                        print("  >>> 已进入锁定，%s 秒后可再答" % d["locked_for"])
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--password", default="", help="管理密码，用于自动作答")
    ap.add_argument("--mode", choices=["peek", "correct", "wrong"], default="peek")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--tier", type=int, default=0, help="选第几档（从 0 开始）")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    code, d = get(base + "/api/health")
    if not d.get("ok"):
        raise SystemExit("服务不可达：%s" % base)
    print("服务：%s　模式=%s　题库=%s 套 / %s 道　受控设备 %s 台"
          % (base, d["mode"], d["banks"], d["questions"], d["managed_devices"]))
    run(base, args.password, args.mode, args.rounds, args.tier)


if __name__ == "__main__":
    main()
