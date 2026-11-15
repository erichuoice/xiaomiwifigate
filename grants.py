#!/usr/bin/env python3
"""放行记录（state/grants.json）的读写与时长换算。

3.50.1 起从 server.py 抽出来，好处是后台首页也能算「今日已得」，
不用再维护两份逻辑。server.py 仍把这些名字导入到自己的命名空间，
所以原有调用点（srv.minutes_left 等）不受影响。
"""

import json
import math
import os
import time

import savings

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
GRANTS_PATH = os.path.join(STATE, "grants.json")


def today():
    return time.strftime("%Y-%m-%d")


def load_grants():
    if not os.path.exists(GRANTS_PATH):
        return {}
    try:
        with open(GRANTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_grants(grants):
    """原子写，避免容器被 SIGTERM 打断时留下半截 JSON。"""
    tmp = GRANTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grants, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GRANTS_PATH)


def minutes_left(rec):
    """剩余分钟；None 表示已设为永久放行，0 表示已过期/从未放行。"""
    if not rec:
        return 0
    expire = rec.get("expire_at")
    if expire is None:
        return 0
    if expire == 0:
        return None
    left = (int(expire) - time.time()) / 60.0
    return max(0, int(math.ceil(left)))


def granted_today(rec):
    if not rec or rec.get("day") != today():
        return 0
    return int(rec.get("today_minutes", 0) or 0)


def earned_today_for(bank_id, rec=None):
    """今日已获得分钟数（每日上限的口径）。

    3.49.0 起答题所得默认先进时间储蓄，所以也要算储蓄里今天存入的部分；
    direct 模式（答对直接放行）照旧只看放行记录，两者取大以防升级当天的过渡。
    """
    by_grant = granted_today(rec)
    by_savings = 0
    if bank_id:
        try:
            by_savings = int(savings.today_earned(bank_id) or 0)
        except Exception:
            by_savings = 0
    return max(by_grant, by_savings)
