#!/usr/bin/env python3
"""路由器放行适配层。目标机型：Xiaomi 路由器 BE7000（RC06）。

mode = mock   : 不碰路由器，只把动作写入日志，用于先把答题流程跑通
mode = miwifi : 调用小米路由器后台内部接口 set_mac_filter 修改 WAN 权限

已在 BE7000 上实测确认（2026-11-08）：
  登录      POST /cgi-bin/luci/api/xqsystem/login
            body: username=admin&password=<明文>&logtype=2
            -> {"code":0,"token":"<stok>","url":"/cgi-bin/luci/;stok=<stok>/web/home"}
  设备列表  GET  /cgi-bin/luci/;stok=<stok>/api/misystem/devicelist
            -> {"mac":"<路由器自身MAC>","list":[{"mac":"..","ip":[{"ip":"192.168.31.x"}],
                "authority":{"wan":1}, ...}]}
  放行/阻断 GET  /cgi-bin/luci/;stok=<stok>/api/xqsystem/set_mac_filter?mac=<MAC>&wan=0|1
            （dry-run 验证过 URL 形态；未实际调用，首次上线前仍需断网实测）

接口来自后台抓包，不是官方开放 API。stok 会过期，因此常驻进程支持
在返回 401/403 时用 admin_password 自动重新登录并重试一次。
"""

import hashlib
import json
import random
import re
import subprocess
import time
from urllib import parse, request

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class RouterError(Exception):
    pass


class TokenExpired(RouterError):
    """登录态失效，需要重新登录（Docker 常驻场景的关键）。"""


def _headers():
    return {
        "User-Agent": "wifi-gate/0.1",
        "Accept": "application/json, text/plain, */*",
    }


def _post(url, data, timeout=6):
    body = parse.urlencode(data).encode("utf-8")
    req = request.Request(url, data=body, headers={
        **_headers(),
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get(url, timeout=6):
    req = request.Request(url, headers=_headers())
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def read_local_arp(ip):
    """从本机 ARP 表按 IP 反查 MAC；服务需与被控设备在同一网段。

    容器必须使用 host 网络，否则 /proc/net/arp 里只有 Docker 网关。
    """
    try:
        with open("/proc/net/arp", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == ip:
                    mac = cols[3].lower()
                    if _MAC_RE.match(mac):
                        return mac
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["arp", "-n", ip], capture_output=True, text=True, timeout=3
        ).stdout
        m = re.search(r"([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})", out)
        if m:
            return ":".join(p.zfill(2) for p in m.group(1).lower().split(":"))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def extract_ips(node):
    """从小米设备列表的各种 IP 写法里提取 IPv4 集合。

    BE7000 实际返回的是嵌套数组："ip": [{"ip": "192.168.31.252", ...}]，
    旧版按字符串直接比较会永远匹配不到，必须递归提取。
    """
    found = set()
    if isinstance(node, dict):
        for k in ("ip", "ipaddr", "ip_addr", "ipv4"):
            v = node.get(k)
            if isinstance(v, str) and _IPV4_RE.match(v.strip()):
                found.add(v.strip())
            elif isinstance(v, (list, dict)):
                found |= extract_ips(v)
    elif isinstance(node, list):
        for item in node:
            found |= extract_ips(item)
    return found


def _mac_of(node):
    for k in ("mac", "macaddr", "mac_addr", "hwaddr"):
        v = node.get(k)
        if isinstance(v, str) and _MAC_RE.match(v.strip()):
            return v.strip().lower().replace("-", ":")
    return None


def _mac_candidates(node, ip, out):
    if isinstance(node, dict):
        mac = _mac_of(node)
        ips = extract_ips(node)
        if mac and ip in ips:
            out.append((len(ips), mac))
        for value in node.values():
            _mac_candidates(value, ip, out)
    elif isinstance(node, list):
        for value in node:
            _mac_candidates(value, ip, out)


def _find_mac_by_ip(node, ip):
    """在设备列表的嵌套结构里，按 IP 找到同层设备的 MAC。

    同一 IP 可能同时出现在「聚合节点」和「单台设备节点」上（聚合节点的 IP
    集合包含全部设备）。取 IP 集合最小的那个候选，才是真正的设备 MAC。
    """
    out = []
    _mac_candidates(node, ip, out)
    if not out:
        return None
    out.sort()
    return out[0][1]


def _wan_candidates(node, ip, out):
    if isinstance(node, dict):
        authority = node.get("authority")
        if isinstance(authority, dict) and authority.get("wan") is not None:
            ips = extract_ips(node)
            if ip in ips:
                out.append((len(ips), authority.get("wan")))
        for value in node.values():
            _wan_candidates(value, ip, out)
    elif isinstance(node, list):
        for value in node:
            _wan_candidates(value, ip, out)


def find_authority_wan(node, ip):
    """读取该 IP 设备当前的 authority.wan（1=允许外网, 0=阻断），用于验证放行是否生效。"""
    out = []
    _wan_candidates(node, ip, out)
    if not out:
        return None
    out.sort(key=lambda x: x[0])
    return out[0][1]


def normalize_devices(data):
    """把小米设备列表归一化成后台可展示的行，按 MAC 去重。"""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            mac = _mac_of(node)
            ips = sorted(extract_ips(node))
            if mac and ips and mac not in found:
                authority = node.get("authority")
                found[mac] = {
                    "ip": ips,
                    "mac": mac,
                    "name": str(node.get("name") or node.get("hostname")
                               or node.get("device_name") or ""),
                    "type": node.get("type"),
                    "online": node.get("online"),
                    "wan": (authority.get("wan")
                            if isinstance(authority, dict) else None),
                }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return sorted(found.values(), key=lambda r: (
        str(r.get("type") or ""), r["ip"][0] if r["ip"] else ""))


def legacy_login_params(base, cfg, password):
    """加密登录的候选实现（BE7000 实测明文登录已够用，此路径仅作备用）。

    后台页面会返回 key: '<32位hex>'，社区常见算法为：
        inner = sha1(password + key)
        pwd   = sha1(nonce + inner)
    """
    page = _get(base.rstrip("/") + "/cgi-bin/luci/web")
    m = re.search(r"\bkey\s*[:=]\s*[\x27\x22]([0-9a-fA-F]{16,})[\x27\x22]", page)
    if not m:
        raise RouterError("后台页面未返回 key，无法构造加密登录")
    key = m.group(1)
    nonce = "0_%s_%d_%d" % ("00" * 6, int(time.time()), random.randint(1000, 9999))
    inner = hashlib.sha1((password + key).encode("utf-8")).hexdigest()
    hashed = hashlib.sha1((nonce + inner).encode("utf-8")).hexdigest()
    return {
        "username": "admin",
        "password": hashed,
        "logtype": 2,
        "nonce": nonce,
    }, key


class MockAdapter:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    def resolve_mac(self, ip):
        """mock 模式没有路由器设备列表，用 IP 推导一个合法的本地 MAC。

        这样后台能像真实设备一样分配题库，自检也能跑完整链路。
        """
        mac = read_local_arp(ip)
        if mac:
            return mac
        h = hashlib.md5(str(ip).encode("utf-8")).hexdigest()
        return "02:%s:%s:%s:%s:%s" % (h[0:2], h[2:4], h[4:6], h[6:8], h[8:10])

    def names_by_mac(self):
        return {}

    def set_wan(self, mac, allow):
        action = "ALLOW" if allow else "BLOCK"
        self.log("[MOCK] %s mac=%s (未修改路由器)" % (action, mac))
        return "mock-ok"

    def describe(self):
        return "mock（只记日志，不改路由器）"


class MiWifiAdapter:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._cached_stok = None
        self._token_expired = False
        self._names = {}
        self._names_ts = 0.0

    def _base(self):
        return self.cfg["base_url"].rstrip("/")

    def _relogin_available(self):
        return bool(str(self.cfg.get("admin_password") or "").strip())

    def get_stok(self, force_refresh=False):
        """优先用手填 stok / 缓存；失效时用 admin_password 重新登录。"""
        manual = str(self.cfg.get("stok") or "").strip()
        if manual and not self._token_expired and not force_refresh:
            return manual
        if self._cached_stok and not force_refresh:
            return self._cached_stok
        pwd = str(self.cfg.get("admin_password") or "")
        if not pwd:
            raise TokenExpired(
                "登录态失效/未配置 stok，且没有 admin_password 可自动重登；"
                "请在 config.json 填 admin_password（BE7000 明文登录已验证）")

        url = self._base() + self.cfg["login_path"]
        # BE7000 实测明文登录即可成功，优先走它；失败才追加加密候选。
        attempts = [("明文", {"username": "admin", "password": pwd, "logtype": 2})]
        last = ""
        tried = 0
        while attempts:
            label, payload = attempts.pop(0)
            tried += 1
            try:
                raw = _post(url, payload)
            except Exception as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
            else:
                last = raw[:200]
                m = re.search(r"stok=([0-9a-zA-Z]+)", raw)
                if not m:
                    try:
                        token = json.loads(raw).get("token")
                        if token:
                            m = re.match(r"^([0-9a-zA-Z]+)$", str(token))
                    except ValueError:
                        pass
                if m:
                    self._cached_stok = m.group(1)
                    self._token_expired = False
                    self.log("登录成功（%s），已取得 stok" % label)
                    return self._cached_stok
            if tried == 1 and not attempts:
                try:
                    legacy, key = legacy_login_params(self._base(), self.cfg, pwd)
                    attempts.append(("加密", legacy))
                    self.log("明文登录失败，追加加密登录候选 key=%s..." % key[:8])
                except Exception as exc:
                    self.log("加密登录候选不可用：%s" % exc)
        raise RouterError(
            "%s 种登录方式都没拿到 stok（最后一次返回：%s）" % (tried, last))

    def _check_token(self, raw):
        """把业务层的 401/403 转成 TokenExpired，便于上层自动重登。"""
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if isinstance(data, dict) and str(data.get("code", "0")) in ("401", "403"):
            self._token_expired = True
            self._cached_stok = None
            raise TokenExpired(
                "路由器返回 code=%s msg=%s" % (data.get("code"), data.get("msg", "")))

    def device_list_raw(self):
        last = ""
        for attempt in (1, 2):
            stok = self.get_stok()
            paths = [self.cfg["device_list_path"]]
            fallback = self.cfg.get("device_list_fallback_path", "")
            if fallback:
                paths.append(fallback)
            for path in paths:
                url = self._base() + path.format(stok=stok)
                try:
                    raw = _get(url)
                except Exception as exc:
                    last = "%s: %s" % (type(exc).__name__, exc)
                    self.log("设备列表 %s 失败：%s"
                             % (path.split("/api/")[-1], last))
                    continue
                try:
                    self._check_token(raw)
                except TokenExpired:
                    # 登录态失效：自动重登一次再试（与 resolve_mac 同策略），
                    # 避免上层拿到「没有这台设备」的误导性错误。
                    self.log("设备列表登录态失效，自动重登（第 %s 次）" % attempt)
                    if attempt == 1 and self._relogin_available():
                        self.get_stok(force_refresh=True)
                        break  # 跳出 path 循环，用新 stok 再试一轮
                    raise
                return raw
        raise RouterError("设备列表接口全部失败：%s" % last)

    def resolve_mac(self, ip):
        for attempt in (1, 2):
            try:
                data = json.loads(self.device_list_raw())
                hit = _find_mac_by_ip(data, ip)
                if hit:
                    return hit
                self.log("设备列表里没有 %s，改用本机 ARP" % ip)
                break
            except TokenExpired as exc:
                self.log("设备列表登录态失效：%s" % exc)
                if attempt == 1 and self._relogin_available():
                    self.get_stok(force_refresh=True)
                    continue
                self.log("无法自动重登，改用本机 ARP")
                break
            except Exception as exc:
                self.log("设备列表解析失败，改用本机 ARP: %s" % exc)
                break
        mac = read_local_arp(ip)
        if not mac:
            raise RouterError("无法解析 %s 的 MAC（设备列表与 ARP 都没查到）" % ip)
        return mac

    def names_by_mac(self):
        """MAC -> 设备名（小米后台里起的名字，如 Huo-2），用于页面选择解锁对象。

        失败不影响主流程：拿不到名字就回落到 MAC。缓存 60 秒，避免每次刷新
        答题页都请求一次路由器。
        """
        now = time.time()
        if self._names and now - self._names_ts < 60:
            return self._names

        def walk(node, out):
            if isinstance(node, dict):
                mac = str(node.get("mac", "")).lower()
                if _MAC_RE.match(mac) and node.get("name"):
                    out.setdefault(mac, str(node["name"]))
                for v in node.values():
                    walk(v, out)
            elif isinstance(node, list):
                for v in node:
                    walk(v, out)

        try:
            out = {}
            walk(json.loads(self.device_list_raw()), out)
            if out:
                self._names = out
                self._names_ts = now
        except Exception as exc:
            self.log("取设备名失败（不影响答题）：%s" % exc)
        return self._names

    def set_wan(self, mac, allow):
        if not mac:
            raise RouterError("MAC 为空，拒绝调用放行接口")
        if self.cfg.get("dry_run"):
            # 演练：真的登录、真的解析 MAC，但不发出修改规则的请求。
            # 用于上线前确认整条链路可用，同时绝不改动家里任何设备的权限。
            self.log("[DRY-RUN] %s mac=%s（演练模式，未调用路由器接口）"
                     % ("ALLOW" if allow else "BLOCK", mac))
            return "dry-run"
        attempts = 2 if self._relogin_available() else 1
        last = None
        for attempt in range(1, attempts + 1):
            try:
                return self._set_wan_once(mac, allow)
            except TokenExpired as exc:
                last = exc
                self.log("放行时登录态失效（第 %s 次）：%s" % (attempt, exc))
                if attempt < attempts:
                    self.log("用 admin_password 重新登录后自动重试一次")
                    self.get_stok(force_refresh=True)
        raise RouterError("放行失败：登录态失效且无法自动恢复（%s）" % last)

    def _set_wan_once(self, mac, allow):
        stok = self.get_stok()
        url = self._base() + self.cfg["set_mac_filter_path"].format(stok=stok)
        wan = self.cfg["wan_allow"] if allow else self.cfg["wan_block"]
        params = {"mac": mac, "wan": wan}
        methods = str(self.cfg.get("set_filter_method", "both")).upper()
        order = ["GET", "POST"] if methods == "BOTH" else [methods]
        last = ""
        for method in order:
            try:
                if method == "GET":
                    raw = _get(url + "?" + parse.urlencode(params))
                else:
                    raw = _post(url, params)
            except Exception as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                self.log("%s 放行调用异常：%s" % (method, last))
                continue
            self.log("[MIWIFI] %s %s %s resp=%s" % (
                "ALLOW" if allow else "BLOCK", method, url, raw[:200]))
            self._check_token(raw)
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and str(data.get("code", "0")) not in ("0", "None"):
                    last = raw[:200]
                    self.log("%s 返回业务错误，尝试下一种调用方式" % method)
                    continue
            except ValueError:
                pass
            return raw
        raise RouterError("放行接口调用失败（最后一次：%s）" % last)

    def describe(self):
        tag = "（演练模式：只登录不改规则）" if self.cfg.get("dry_run") else ""
        return "miwifi %s（%s）%s" % (
            self.cfg.get("model", ""), self._base(), tag)


def build(cfg, log):
    if cfg.get("mode") == "miwifi":
        return MiWifiAdapter(cfg, log)
    return MockAdapter(cfg, log)
