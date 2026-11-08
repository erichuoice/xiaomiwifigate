#!/usr/bin/env python3
"""小米路由器接口只读探测：不会修改任何规则。目标机型 BE7000（RC06）。

用法：
  python3 probe_miwifi.py                             # 识别机型 + 结构探测
  python3 probe_miwifi.py --password '管理密码'        # 验证自动登录并读取设备列表
  python3 probe_miwifi.py --stok <从后台URL复制>      # 跳过登录
  python3 probe_miwifi.py --check-ip 192.168.31.252  # 验证该 IP 能否反查到 MAC

全程只读：登录接口只用于取得 stok；放行接口只打印 URL，不实际调用。
"""

import argparse
import json
import os
import re
import sys
from urllib import parse, request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import router_adapter  # noqa: E402

with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)["router"]

base_url = CFG["base_url"].rstrip("/")


def line(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def get(url, timeout=6):
    req = request.Request(url, headers={"User-Agent": "wifi-gate-probe/0.1"})
    with request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post(url, data, timeout=6):
    body = parse.urlencode(data).encode("utf-8")
    req = request.Request(url, data=body, headers={
        "User-Agent": "wifi-gate-probe/0.1",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def collect(node, out):
    """把设备列表归一化：ip 可能是嵌套数组，authority.wan 是当前外网权限。"""
    if isinstance(node, dict):
        mac = router_adapter._mac_of(node)
        ips = sorted(router_adapter.extract_ips(node))
        if mac and ips:
            authority = node.get("authority")
            row = {
                "ip": ips,
                "mac": mac,
                "name": node.get("name") or node.get("hostname") or node.get("device_name") or "",
                "type": node.get("type"),
                "online": node.get("online"),
            }
            if isinstance(authority, dict):
                row["wan"] = authority.get("wan")
            out.append(row)
        for v in node.values():
            collect(v, out)
    elif isinstance(node, list):
        for v in node:
            collect(v, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stok", default="", help="从路由器后台 URL 里复制的 stok")
    ap.add_argument("--password", default="", help="路由器管理密码")
    ap.add_argument("--check-ip", default="", help="验证该 IP 能否反查到 MAC")
    args = ap.parse_args()

    password = args.password or CFG.get("admin_password", "")
    stok = args.stok or str(CFG.get("stok", "")).strip()
    check_ip = args.check_ip

    line("1 / 5 路由器可达性与机型识别")
    try:
        code, body = get(base_url)
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        print("HTTP %s，返回长度 %s 字节，页面标题：%s" % (
            code, len(body), title.group(1).strip() if title else "无"))
        _, login_page = get(base_url + "/cgi-bin/luci/web")
        models = sorted(set(m.lower() for m in re.findall(
            r"\b(?:xiaomi\.router\.)?(rc\d{2,4}|be\d{4}|ax\d{4})\b",
            login_page, re.I)))
        print("登录页型号标识：%s" % (models[:8] if models else "未发现"))
        if "rc06" in models or "be7000" in models:
            print("已确认机型：Xiaomi 路由器 BE7000 / 小米路由器 7000（RC06）")
        key_hit = re.search(
            r"\bkey\s*[:=]\s*[\x27\x22]([0-9a-fA-F]{16,})[\x27\x22]", login_page)
        print("登录页加密 key：%s（明文登录可用时不需要它）" % (
            key_hit.group(1)[:8] + "..." if key_hit else "未发现"))
    except Exception as exc:
        print("不可达或识别失败：%s: %s" % (type(exc).__name__, exc))
        print("请确认本机连的是这台小米路由器的 Wi-Fi，且地址是 %s" % base_url)
        return

    if not stok:
        line("2 / 5 尝试登录获取 stok")
        if not password:
            print("未提供管理密码，跳过真实登录。可用 --password 或 --stok 重跑。")
        else:
            login_url = base_url + CFG["login_path"]
            variants = [
                {"username": "admin", "password": password, "logtype": 2},
                {"username": "admin", "password": password},
                {"password": password, "logtype": 2},
            ]
            for i, payload in enumerate(variants, 1):
                safe = dict(payload)
                safe["password"] = "***"
                try:
                    code, body = post(login_url, payload)
                    print("变体 %s %s -> HTTP %s: %s" % (i, safe, code, body[:300]))
                    m = re.search(r"stok=([0-9a-zA-Z]+)", body)
                    if not m:
                        try:
                            token = json.loads(body).get("token")
                            if token:
                                m = re.match(r"^([0-9a-zA-Z]+)$", str(token))
                        except ValueError:
                            pass
                    if m:
                        stok = m.group(1)
                        print("已取得 stok = %s" % stok)
                        print("结论：明文登录可用，常驻进程可自动刷新 stok，无需解锁 SSH。")
                        break
                except Exception as exc:
                    print("变体 %s %s -> %s: %s" % (i, safe, type(exc).__name__, exc))
            if not stok:
                try:
                    legacy, key = router_adapter.legacy_login_params(base_url, CFG, password)
                    code, body = post(login_url, legacy)
                    print("候选（加密登录）key=%s... -> HTTP %s: %s" % (
                        key[:8], code, body[:300]))
                    m = re.search(r"stok=([0-9a-zA-Z]+)", body)
                    if m:
                        stok = m.group(1)
                        print("已取得 stok = %s" % stok)
                except Exception as exc:
                    print("加密登录候选不可用：%s: %s" % (type(exc).__name__, exc))
            if not stok:
                print("\n几种登录方式都没拿到 stok。请把上面输出整段贴回来再适配。")
    else:
        line("2 / 5 使用手填 stok")
        print("stok = %s" % stok)

    if not stok:
        print("\n后续步骤跳过。用 --password 或 --stok 重跑即可继续。")
        return

    line("3 / 5 读取在线设备列表")
    devices = []
    last_data = None
    list_paths = [CFG["device_list_path"]]
    if CFG.get("device_list_fallback_path"):
        list_paths.append(CFG["device_list_fallback_path"])
    for path in list_paths:
        list_url = base_url + path.format(stok=stok)
        try:
            code, body = get(list_url)
            print("URL  %s" % list_url)
            print("HTTP %s，返回前 300 字符：%s" % (code, body[:300]))
            data = json.loads(body)
            if isinstance(data, dict) and str(data.get("code", "0")) not in ("0", "None"):
                print("提示：业务返回 code=%s msg=%s" % (data.get("code"), data.get("msg", "")))
                if str(data.get("code")) in ("401", "403"):
                    print("stok 无效或已过期 —— 重新登录后台复制 stok 再试。")
                    continue
            last_data = data
            collect(data, devices)
            print("本路径解析到 %s 条设备记录" % len(devices))
            if devices:
                break
        except Exception as exc:
            print("该路径失败：%s: %s" % (type(exc).__name__, exc))

    print("\n共解析到 %s 条设备记录（wan: 1=可上外网, 0=被阻断）：" % len(devices))
    for d in devices[:30]:
        print("  " + json.dumps(d, ensure_ascii=False))
    personal = [d for d in devices if d.get("type") == 2]
    if personal:
        print("\n手机 / 平板 / 电脑类设备（type=2），可作为答题测试目标：")
        for d in personal:
            print("  %-18s %-20s %s" % (
                ",".join(d["ip"]), d["mac"], d["name"]))

    line("4 / 5 反查 MAC（验证服务端真实解析路径）")
    target = check_ip or ""
    if not target:
        print("未指定 --check-ip。例如iPad可跑：")
        print("  python3 probe_miwifi.py --password '***' --check-ip 192.168.31.252")
    else:
        print("目标 IP：%s" % target)
        if last_data is not None:
            hit = router_adapter._find_mac_by_ip(last_data, target)
            print("服务端 _find_mac_by_ip 结果：%s" % (hit or "未命中"))
            wan = router_adapter.find_authority_wan(last_data, target)
            print("该设备当前 authority.wan：%s" % ("未读到" if wan is None else wan))
        matched = next((d for d in devices if target in d["ip"]), None)
        print("设备列表归一化命中：%s" % (
            json.dumps(matched, ensure_ascii=False) if matched else "无"))
        local = router_adapter.read_local_arp(target)
        print("本机 ARP 表结果：%s（容器内需 host 网络才能读到宿主机 ARP）" % (
            local or "未查到"))
        print("服务端最终会用的 MAC：%s" % (
            (matched or {}).get("mac") or local or "查不到，放行会失败"))

    line("5 / 5 放行接口 dry-run（不会真的执行）")
    set_url = base_url + CFG["set_mac_filter_path"].format(stok=stok)
    print("GET 优先：%s?mac=<MAC>&wan=%s" % (set_url, CFG["wan_allow"]))
    print("POST 回退：%s   body: mac=<MAC>&wan=%s" % (set_url, CFG["wan_allow"]))
    print("wan=%s = 放行，wan=%s = 阻断" % (CFG["wan_allow"], CFG["wan_block"]))
    print("\n验证办法：在小米 WiFi App 对测试设备点「暂停联网」，再跑本脚本，")
    print("看该设备 authority.wan 是否变成 0，以及局域网内还能否打开答题页。")
    print("本脚本不修改任何规则；把 config.json 的 router.mode 改为 miwifi 才会真正生效。")


if __name__ == "__main__":
    main()
