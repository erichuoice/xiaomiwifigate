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
CONFIG = os.path.join(BASE, "config.json")
DEFAULT = os.path.join(BASE, "config.default.json")
STATE = os.path.join(BASE, "state")
RUNTIME_CONFIG = os.path.join(tempfile.gettempdir(), "wifigate-config.json")


def prepare():
    if not os.path.exists(CONFIG):
        shutil.copyfile(DEFAULT, CONFIG)
        print("[init] 未挂载 config.json，已复制镜像内默认配置（mock 模式）", flush=True)
    os.makedirs(STATE, exist_ok=True)


def load_cfg():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def check():
    prepare()
    cfg = load_cfg()
    s, r = cfg["server"], cfg["router"]
    st = os.stat(CONFIG)
    print("配置文件       : %s" % CONFIG)
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
    print("config 属主/权限: uid=%s gid=%s mode=%s" % (
        st.st_uid, st.st_gid, oct(st.st_mode & 0o777)))
    print("配置读取方式   : 启动时复制到容器内 %s，config.json 保持 600 也可读" % RUNTIME_CONFIG)
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
