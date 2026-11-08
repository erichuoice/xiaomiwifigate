#!/usr/bin/env python3
"""容器入口：准备配置 / 目录权限，降权后 exec 主进程。纯标准库。

也可用 python3 docker-entrypoint.py --check 在宿主机做一次部署前检查（不启动服务）。

关键点：宿主机 config.json 通常是 600（内含管理密码），而容器降权后的
PUID 未必等于文件属主。因此以 root 读取一次，复制到容器内 /tmp 的
600 临时文件并 chown 给降权用户，再通过 WIFIGATE_CONFIG 传给服务进程。
密码不会落到挂载目录，也不会进镜像层。
"""

import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("WIFIGATE_CONFIG") or os.path.join(BASE, "config.json")
DEFAULT = os.path.join(BASE, "config.default.json")
STATE = os.path.join(BASE, "state")
RUNTIME_CONFIG = os.path.join(tempfile.gettempdir(), "wifigate-config.json")


def prepare():
    """准备状态目录与配置占位。

    没有 config.json 时**不再**复制一份「能直接用」的配置，而是放一份空密码模板：
    服务据此判定「尚未配置」，所有页面跳转到 /setup 引导页。
    """
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(CONFIG):
        return
    try:
        with open(DEFAULT, encoding="utf-8") as f:
            data = f.read()
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(CONFIG, 0o600)
        print("[init] 未找到 config.json，已放一份待配置模板（打开网页填密码）", flush=True)
    except OSError as exc:
        print("[init] 无法写入 %s（%s），将以引导模式运行" % (CONFIG, exc), flush=True)


def needs_setup():
    """配置里没有路由器密码 = 还没配置过。"""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return True
    return not str((cfg.get("router") or {}).get("admin_password") or "").strip()


def load_default():
    with open(DEFAULT, encoding="utf-8") as f:
        return json.load(f)


def load_cfg():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def check():
    exists = os.path.exists(CONFIG)
    cfg = load_cfg() if exists else load_default()
    s, r = cfg["server"], cfg["router"]
    print("配置文件       : %s（%s）" % (
        CONFIG, "已存在" if exists else "不存在，首次启动会进入网页引导 /setup"))
    print("监听地址       : %s:%s" % (s["host"], s["port"]))
    print("路由器模式     : %s" % r.get("mode"))
    print("已配置管理密码 : %s" % ("是" if r.get("admin_password") else "否"))
    print("已手填 stok    : %s" % ("是" if r.get("stok") else "否"))
    print("信任 XFF       : %s" % s.get("trust_x_forwarded_for", False))
    print("后台管理密码   : %s" % ("已设置" if s.get("admin_password") else "未设置，后台将被禁用"))
    print("放行分钟数     : %s（后台可改，0=一直放行）" % s.get("reblock_minutes", 60))
    print("state 目录     : %s（可写：%s）" % (STATE, os.access(STATE, os.W_OK)))
    runtime = os.path.join(STATE, "runtime.json")
    print("运行时配置     : %s（%s）" % (
        runtime, "已存在" if os.path.exists(runtime) else "启动后自动创建"))
    print("目标 UID/GID   : %s/%s（只影响 state 目录写入）" % (
        os.environ.get("PUID", "1000"), os.environ.get("PGID", "1000")))
    if exists:
        st = os.stat(CONFIG)
        print("config 属主/权限: uid=%s gid=%s mode=%s" % (
            st.st_uid, st.st_gid, oct(st.st_mode & 0o777)))
        print("配置读取方式   : 复制到容器内 %s，config.json 保持 600 也可读" % RUNTIME_CONFIG)
    else:
        print("配置读取方式   : 引导模式，直接读写 %s（填完密码后落盘到此）" % CONFIG)
    print("提醒           : 必须用 network_mode: host，否则客户端 IP 会被 NAT")
    if r.get("mode") == "mock":
        print("当前是 mock 模式，答对只写日志，不会改动路由器。")


def target_ids():
    if os.geteuid() != 0:
        return os.geteuid(), os.getegid()
    return int(os.environ.get("PUID") or 1000), int(os.environ.get("PGID") or 1000)


def prepare_runtime_config(uid, gid):
    """以启动时的权限读取只读挂载，复制成降权用户可读的容器内临时文件。"""
    with open(CONFIG, "rb") as f:
        data = f.read()
    fd = os.open(RUNTIME_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.chown(RUNTIME_CONFIG, uid, gid)
    except OSError:
        pass
    return RUNTIME_CONFIG


def drop_privileges(uid, gid):
    if os.geteuid() != 0:
        return
    # state 是宿主机 bind mount，把属主改成目标 UID，避免 NAS 上权限不符导致写日志失败
    for root, dirs, files in os.walk(STATE):
        for path in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass
    try:
        os.setgroups([])
    except OSError:
        pass
    os.setgid(gid)
    os.setuid(uid)
    print("[init] 已降权到 uid=%s gid=%s" % (uid, gid), flush=True)


def main():
    if "--check" in sys.argv[1:]:
        check()
        return
    prepare()
    uid, gid = target_ids()
    if needs_setup():
        # 引导模式：必须指向真实路径，否则 /setup 填的密码会落在临时文件里、重启即丢
        try:
            os.chown(CONFIG, uid, gid)
        except OSError:
            pass
        os.environ["WIFIGATE_CONFIG"] = CONFIG
        print("[init] 尚未配置，以引导模式启动：打开网页会进入 /setup", flush=True)
    else:
        os.environ["WIFIGATE_CONFIG"] = prepare_runtime_config(uid, gid)
    drop_privileges(uid, gid)
    cmd = list(sys.argv[1:])
    if cmd and cmd[0] in ("python3", "python"):
        cmd.pop(0)
    if not cmd:
        cmd = [os.path.join(BASE, "server.py")]
    os.execv(sys.executable, [sys.executable] + cmd)


if __name__ == "__main__":
    main()
