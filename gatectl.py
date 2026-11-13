#!/usr/bin/env python3
"""gatectl：不开网页后台，命令行直接看设备状态 / 断网 / 放行。

用法（在 wifi-gate 所在机器或容器里）：
    python3 gatectl.py list                # 看所有受控设备状态
    python3 gatectl.py off  <设备>         # 立即断网
    python3 gatectl.py on   <设备>         # 放行（默认永久）
    python3 gatectl.py on   <设备> 30      # 放行 30 分钟，到点自动断

<设备> 可以是：设备名（可只打一部分，不分大小写）/ MAC（可只打前几位）。
例如：python3 gatectl.py off huo-2

路由器上的用法（配合 SSH 通道）：
    ssh router "docker exec wifi-gate python3 gatectl.py list"
    ssh router "docker exec wifi-gate python3 gatectl.py off huo-2"
"""

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def _host():
    """默认连本机服务；也支持 WIFIGATE_HOST 环境变量指定别处。"""
    env = (os.environ.get("WIFIGATE_HOST") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        cfg = json.load(open(os.path.join(BASE, "config.json"),
                             encoding="utf-8"))
        port = int(cfg.get("server", {}).get("port", 8080))
    except (OSError, ValueError):
        port = 8080
    return "http://127.0.0.1:%s" % port


def _password():
    try:
        cfg = json.load(open(os.path.join(BASE, "config.json"),
                             encoding="utf-8"))
        return cfg.get("server", {}).get("admin_password") or ""
    except (OSError, ValueError):
        return ""


HOST = _host()
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_jar))


def call(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        HOST + path, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {}
    except Exception as exc:
        return 0, {"detail": "连接失败：%s（服务在跑吗？地址 %s）" % (exc, HOST)}


def login():
    pw = _password()
    code, res = call("/api/admin/login", {"password": pw})
    if code != 200 or not res.get("ok"):
        print("登录失败：%s" % res.get("detail", code))
        sys.exit(1)


def devices():
    code, res = call("/api/admin/device/wan")
    if code != 200 or not res.get("ok"):
        print("读取设备失败：%s" % res.get("detail", code))
        sys.exit(1)
    return res.get("devices") or []


def pick(rows, target):
    """按 MAC 精确 / MAC 前缀 / 名称精确 / 名称包含 匹配设备。"""
    t = target.strip().lower()
    for r in rows:
        if r["mac"].lower() == t:
            return r
    if len(t) >= 4:
        for r in rows:
            if r["mac"].lower().startswith(t):
                return r
    for r in rows:
        if t == (r.get("name") or "").lower():
            return r
    for r in rows:
        if t in (r.get("name") or "").lower():
            return r
    return None


def show_list(rows):
    if not rows:
        print("当前没有受控设备（后台分配题库后才会出现在这里）")
        return
    print("%-12s %-12s %-12s %-12s %-16s %s"
          % ("状态", "剩余", "今日累计", "题库", "设备", "MAC"))
    for r in rows:
        left = r.get("remaining_minutes")
        left_txt = "永久" if left is None else "%s分钟" % left
        print("%-12s %-12s %-12s %-12s %-16s %s"
              % (r.get("state"), left_txt,
                 "%s分钟" % r.get("today_minutes", 0),
                 r.get("bank_name"),
                 r.get("name") or "（未取名）", r.get("mac")))
    print("\n断网：gatectl.py off <设备>    放行：gatectl.py on <设备> [分钟]（不给分钟=永久）")


def main():
    args = sys.argv[1:]
    if not args or args[0].lower() in ("list", "ls", "status"):
        login()
        show_list(devices())
        return
    cmd = args[0].lower()
    if cmd not in ("on", "off"):
        print(__doc__)
        sys.exit(1)
    if len(args) < 2:
        print("用法：gatectl.py %s <设备名/MAC> [分钟]" % cmd)
        sys.exit(1)
    target = args[1]
    minutes = 0
    if cmd == "on" and len(args) >= 3:
        try:
            minutes = max(0, int(args[2]))
        except ValueError:
            print("分钟数请用整数：%s" % args[2])
            sys.exit(1)
    login()
    rows = devices()
    row = pick(rows, target)
    if not row:
        print("找不到设备「%s」。当前受控设备：" % target)
        for r in rows:
            print("  %-16s %s" % (r.get("name") or "（未取名）", r.get("mac")))
        sys.exit(1)
    mac = row["mac"]
    name = row.get("name") or mac
    want = cmd == "on"
    code, res = call("/api/admin/device/wan",
                      {"mac": mac, "wan": want, "minutes": minutes})
    if code != 200 or not res.get("ok"):
        print("操作失败：%s" % res.get("detail", code))
        sys.exit(1)
    if want:
        if minutes == 0:
            print("已放行「%s」，永久有效。" % name)
        else:
            print("已放行「%s」，%s 分钟后自动断网。" % (name, minutes))
    else:
        print("已断网「%s」。它答一轮题即可恢复上网。" % name)


if __name__ == "__main__":
    main()
