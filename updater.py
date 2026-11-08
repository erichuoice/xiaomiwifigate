#!/usr/bin/env python3
"""从 GitHub 拉最新代码并原地替换，实现后台一键更新 / 定时自动更新。

设计约束：
- 只替换代码文件，``config.json`` 与 ``state/`` 永不触碰（密码、题库、授权记录都在里面）
- 替换前先把「即将被覆盖的文件」打包到 ``state/backup/``，出问题可以直接拷回来
- 更新过程只写同目录下的临时文件再 ``os.replace``，不会出现半截文件
- 成功后由调用方重启进程（容器里 execv 即可，不依赖 restart 策略）
"""

import io
import json
import os
import sys
import tarfile
import threading
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(BASE, "VERSION")
STATE = os.path.join(BASE, "state")
RECORD_PATH = os.path.join(STATE, "update.json")
BACKUP_DIR = os.path.join(STATE, "backup")

REPO = "erichuoice/xiaomiwifigate"
BRANCH = "main"
RAW_VERSION = "https://raw.githubusercontent.com/%s/%s/VERSION" % (REPO, BRANCH)
TARBALL = "https://github.com/%s/archive/refs/heads/%s.tar.gz" % (REPO, BRANCH)

# 路由器直连 GitHub 基本不通，默认先走加速站；可用 WIFIGATE_MIRROR 覆盖（逗号分隔，
# 写 direct 表示直连），例如：
#   -e WIFIGATE_MIRROR=https://gh-proxy.com/,direct
DEFAULT_MIRRORS = ("https://gh-proxy.com/", "")

PROTECTED_FILES = {"config.json"}
PROTECTED_DIRS = {"state", "config", ".git", "__pycache__", ".workbuddy", "node_modules"}

MAX_BACKUPS = 5


class UpdaterError(Exception):
    pass


# ---- 版本 ----

def version():
    try:
        with open(VERSION_PATH, encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _tuple(text):
    out = []
    for part in str(text).strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits or 0))
    return tuple(out) or (0,)


def is_newer(candidate, base):
    return _tuple(candidate) > _tuple(base)


# ---- 网络 ----

def mirrors():
    raw = os.environ.get("WIFIGATE_MIRROR")
    if raw is None:
        return DEFAULT_MIRRORS
    out = []
    for part in raw.split(","):
        p = part.strip()
        if p in ("", "direct"):
            out.append("")
        elif p:
            out.append(p if p.endswith("/") else p + "/")
    return tuple(out) or DEFAULT_MIRRORS


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "wifi-gate-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---- 记录 ----

def read_record():
    try:
        with open(RECORD_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_record(patch):
    rec = read_record()
    rec.update(patch)
    rec["current_after_restart"] = version()
    try:
        os.makedirs(STATE, exist_ok=True)
        tmp = RECORD_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RECORD_PATH)
    except OSError:
        pass
    return rec


# ---- 检查 / 安装 ----

def check(timeout=10):
    """比对远端 VERSION 与本地版本，结果写入 state/update.json。"""
    cur = version()
    errors = []
    for mirror in mirrors():
        name = mirror or "直连"
        try:
            text = _fetch(mirror + RAW_VERSION, timeout).decode("utf-8", "replace")
            latest = (text.strip().splitlines() or [""])[0].strip()
            if not latest or not latest[0].isdigit():
                raise UpdaterError("版本文件内容异常：%r" % text[:30])
            return _write_record({
                "current": cur, "latest": latest,
                "newer": is_newer(latest, cur),
                "mirror": name, "checked_at": int(time.time()), "error": "",
            })
        except Exception as exc:  # 换下一个源
            errors.append("%s：%s" % (name, exc))
    return _write_record({
        "current": cur, "latest": "", "newer": False, "mirror": "",
        "checked_at": int(time.time()), "error": "；".join(errors)[:300],
    })


def _protected(rel):
    if rel in PROTECTED_FILES:
        return True
    return rel.split("/")[0] in PROTECTED_DIRS


def _unpack(blob):
    """把 tarball 读成 {相对路径: 字节}，顺便取出版本号。"""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
    except Exception as exc:
        raise UpdaterError("不是合法的 tar.gz：%s" % exc)
    with tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        if not members:
            raise UpdaterError("压缩包是空的")
        roots = {m.name.split("/", 1)[0] for m in members}
        if len(roots) != 1:
            raise UpdaterError("压缩包结构异常（顶层目录不唯一）")
        files = {}
        for m in members:
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not rel or _protected(rel):
                continue
            if rel.startswith("/") or ".." in rel.split("/"):
                raise UpdaterError("压缩包里出现非法路径：%s" % m.name)
            handle = tf.extractfile(m)
            files[rel] = handle.read() if handle else b""
    if "server.py" not in files:
        raise UpdaterError("压缩包里没有 server.py，拒绝更新")
    text = files.get("VERSION", b"").decode("utf-8", "replace").strip()
    return files, (text or "0.0.0")


def _backup(from_version, names):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, "%s-%s.tar.gz" % (from_version, stamp))
    with tarfile.open(path, "w:gz") as tf:
        for rel in sorted(names):
            full = os.path.join(BASE, rel)
            if os.path.isfile(full):
                tf.add(full, arcname=rel)
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz"))
    for name in olds[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError:
            pass
    return os.path.join("state/backup", os.path.basename(path))


def _copy_in(files):
    for rel, data in files.items():
        full = os.path.join(BASE, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = full + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, full)


def install(expect=""):
    """下载并覆盖代码文件。expect 非空时要求远端版本与之一致。"""
    cur = version()
    errors = []
    for mirror in mirrors():
        name = mirror or "直连"
        try:
            blob = _fetch(mirror + TARBALL, timeout=90)
        except Exception as exc:
            errors.append("%s：下载失败 %s" % (name, exc))
            continue
        try:
            files, new_ver = _unpack(blob)
        except UpdaterError as exc:
            errors.append("%s：%s" % (name, exc))
            continue
        if expect and new_ver != expect:
            errors.append("%s：版本不符（拿到 %s，期望 %s）" % (name, new_ver, expect))
            continue
        backup = _backup(cur, files.keys())
        _copy_in(files)
        return _write_record({
            "from": cur, "to": new_ver, "at": int(time.time()),
            "files": len(files), "backup": backup,
            "mirror": name, "error": "",
        })
    raise UpdaterError("；".join(errors)[:400])


def schedule_restart(delay=2.0):
    """延迟一点再 execv，让当前请求先把响应发出去。"""
    def _go():
        try:
            os.chdir(BASE)
        except OSError:
            pass
        os.execv(sys.executable, [sys.executable, os.path.join(BASE, "server.py")])

    timer = threading.Timer(delay, _go)
    timer.daemon = True
    timer.start()


def status():
    rec = read_record()
    cur = version()
    return {
        "current": cur,
        "latest": rec.get("latest") or "",
        "newer": bool(rec.get("newer")) and is_newer(rec.get("latest") or "", cur),
        "checked_at": rec.get("checked_at") or 0,
        "error": rec.get("error") or "",
        "last_install": {
            "from": rec.get("from") or "",
            "to": rec.get("to") or "",
            "at": rec.get("at") or 0,
            "files": rec.get("files") or 0,
            "backup": rec.get("backup") or "",
        },
        "mirrors": [m or "直连" for m in mirrors()],
    }
