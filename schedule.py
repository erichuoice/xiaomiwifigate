#!/usr/bin/env python3
"""设备答题时段：每台设备可按周一~周日配置多个「可以答题放行」的时间段。

规则只约束「答题解锁」这条路径：
  · 时段内  —— 可以答题，放行时长最多到本时段结束（不会跨到禁止时段）
  · 时段外  —— 不能开始答题，也不能通过答题放行，已有放行会被自动回收
  · 家长在后台手动点「放行」属于临时破例，不受时段限制

时间按服务所在时区解释（容器已设 TZ=Asia/Shanghai），
格式 HH:MM，支持 22:00-06:00 这种跨午夜时段，00:00-24:00 表示全天。
留空的星期 = 当天不允许答题；整周都留空 = 不限制（等同没配规则）。
"""

import re
import time

WEEK_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DAY_MINUTES = 24 * 60
MAX_WINDOWS_PER_DAY = 8

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*[-~—至]\s*(\d{1,2}:\d{2})\s*$")
_SPLIT_RE = re.compile(r"[,，;；\n]+")


class ScheduleError(ValueError):
    """规则格式不合法。"""


class OutsideSchedule(Exception):
    """当前不在允许答题的时段。"""


def _minutes(text, allow_24=False):
    m = _HHMM_RE.match(str(text).strip())
    if not m:
        raise ScheduleError("时间要写成 18:00 这种格式：%s" % text)
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute > 59 or hour > 24 or (hour == 24 and minute != 0):
        raise ScheduleError("时间不合法：%s" % text)
    total = hour * 60 + minute
    if hour == 24 and not allow_24:
        raise ScheduleError("24:00 只能作为结束时间：%s" % text)
    return total


def _fmt(total):
    return "%02d:%02d" % (total // 60, total % 60)


def parse_window(raw):
    """把一个时间段规范化成 ["开始","结束"]；start>end 表示跨午夜。"""
    if isinstance(raw, str):
        m = _RANGE_RE.match(raw)
        if not m:
            raise ScheduleError("时间段要写成 18:00-20:00：%s" % raw)
        start, end = m.group(1), m.group(2)
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start, end = raw[0], raw[1]
    else:
        raise ScheduleError("时间段格式不正确：%r" % (raw,))
    s = _minutes(start)
    e = _minutes(end, allow_24=True)
    if s == e:
        raise ScheduleError("开始和结束时间不能相同：%s" % raw)
    if s > e and e == DAY_MINUTES:
        raise ScheduleError("跨夜时段的结束时间不能写 24:00：%s" % raw)
    return [_fmt(s), _fmt(e)]


def _day_items(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        return [x for x in _SPLIT_RE.split(raw) if x.strip()]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise ScheduleError("每天的时间段格式不正确")


def normalize(data):
    """校验并规范化单台设备的规则。输出 7 天、每天若干个 [start,end]。"""
    if not isinstance(data, dict):
        raise ScheduleError("时段规则格式不正确")
    enabled = bool(data.get("enabled", True))
    raw_days = data.get("days", [])
    if isinstance(raw_days, dict):
        rows = [raw_days.get(str(i), raw_days.get(WEEK_NAMES[i], []))
                for i in range(7)]
    elif isinstance(raw_days, (list, tuple)):
        rows = list(raw_days[:7]) + [[] for _ in range(max(0, 7 - len(raw_days)))]
    else:
        raise ScheduleError("每周时段格式不正确")
    days = []
    for i, raw in enumerate(rows):
        windows = []
        for item in _day_items(raw):
            w = parse_window(item)
            if w not in windows:
                windows.append(w)
        if len(windows) > MAX_WINDOWS_PER_DAY:
            raise ScheduleError("%s 最多设 %s 个时间段" %
                                (WEEK_NAMES[i], MAX_WINDOWS_PER_DAY))
        windows.sort(key=lambda w: _minutes(w[0]))
        days.append(windows)
    return {"enabled": enabled, "days": days}


def normalize_all(raw):
    """整份配置（mac → 规则）；坏条目直接忽略，避免一条脏数据拖垮服务。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for mac, rule in raw.items():
        try:
            out[str(mac).strip().lower()] = normalize(rule)
        except (ScheduleError, TypeError, ValueError):
            continue
    return out


def limited(sched):
    """规则是否真的生效（启用且至少填了一个时间段）。"""
    return bool(sched) and bool(sched.get("enabled")) and \
        any(sched.get("days") or [])


def _blank_state(ts, wday, minute, sched):
    return {
        "enabled": bool(sched) and bool(sched.get("enabled")),
        "limited": False, "allowed": True, "unlimited": True,
        "ends_at": None, "ends_hhmm": None,
        "next_start": None, "next_label": None, "change_at": None,
        "day": wday, "minute": minute, "now": ts,
        "today_text": "未设置", "summary": summary(sched),
    }


def window_state(sched, ts=None):
    """算当前时刻是否允许答题，以及状态下一次翻转的时间。

    返回字段：
      limited/allowed/unlimited  规则状态
      ends_at / ends_hhmm        本时段结束（用于夹住放行时长）
      next_start / next_label    下次可答题时间（给提示用）
      change_at                  下次状态翻转时刻（前端据此自动刷新）
      today_text / summary       展示用文字
    """
    ts = time.time() if ts is None else float(ts)
    local = time.localtime(ts)
    wday = local.tm_wday                       # 0 = 周一
    minute = local.tm_hour * 60 + local.tm_min
    midnight = ts - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
    if not limited(sched):
        return _blank_state(ts, wday, minute, sched)

    days = sched["days"]
    ends = []
    starts = []
    # 跨午夜窗口（如昨天 22:00-06:00）会延续到今天凌晨
    prev_index = (wday - 1) % 7
    for w in (days[prev_index] if prev_index < len(days) else []):
        s, e = _minutes(w[0]), _minutes(w[1], allow_24=True)
        if s > e and minute < e:
            ends.append(midnight + e * 60)
    for offset in range(8):                     # 今天 + 未来 7 天，覆盖整周
        base = midnight + offset * 86400
        day_index = (wday + offset) % 7
        for w in (days[day_index] if day_index < len(days) else []):
            s, e = _minutes(w[0]), _minutes(w[1], allow_24=True)
            begin = base + s * 60
            if begin > ts + 1:
                starts.append(begin)
            if offset != 0:
                continue
            if s < e:                            # 当天内的时段
                if s <= minute < e:
                    ends.append(base + e * 60)
            elif minute >= s:                    # 跨午夜：当晚开始，次日结束
                ends.append(base + 86400 + e * 60)

    allowed = bool(ends)
    ends_at = max(ends) if ends else None
    next_start = min(starts) if starts else None
    change_at = ends_at if allowed else next_start
    today_wins = days[wday] if wday < len(days) else []
    return {
        "enabled": True, "limited": True,
        "allowed": allowed, "unlimited": False,
        "ends_at": ends_at,
        "ends_hhmm": time.strftime("%H:%M", time.localtime(ends_at))
                     if ends_at else None,
        "next_start": next_start,
        "next_label": ("%s %s" % (
            WEEK_NAMES[time.localtime(next_start).tm_wday],
            time.strftime("%H:%M", time.localtime(next_start))))
            if next_start else None,
        "change_at": change_at,
        "day": wday, "minute": minute, "now": ts,
        "today_text": "、".join("%s-%s" % (w[0], w[1]) for w in today_wins)
                      or "今天不开放",
        "summary": summary(sched),
    }


def summary(sched):
    """一周规则的可读摘要，尽量合并相同的天。"""
    if not limited(sched):
        return "未设置（随时可答题）"
    days = sched["days"]
    parts = []
    for i in range(7):
        wins = days[i] if i < len(days) else []
        parts.append("、".join("%s-%s" % (w[0], w[1]) for w in wins))
    nonempty = [p for p in parts if p]
    if len(nonempty) == 7 and len(set(parts)) == 1:
        return "每天 " + parts[0]
    if (parts[0] and len(set(parts[:5])) == 1 and
            parts[5] == parts[6]):
        return "周一至周五 %s；周六、周日 %s" % (parts[0], parts[5] or "不开放")
    return "；".join("%s %s" % (WEEK_NAMES[i], parts[i])
                    for i in range(7) if parts[i]) or "整周不开放"


def hint(st):
    """给答题页看的时段说明。"""
    if not st or st.get("unlimited"):
        return ""
    if st.get("allowed"):
        return "当前可答题，本时段到 %s 结束" % st["ends_hhmm"]
    if st.get("next_label"):
        return "现在不在答题时间（%s）。下次可答题：%s" % (
            st.get("today_text"), st["next_label"])
    return "现在不在答题时间（%s）" % st.get("today_text")


def clamp_expire(expire, sched, ts=None):
    """把放行到期时间夹在本时段结束之前；时段外拒绝放行。

    返回 (expire_at, ends_hhmm)。expire_at == 0 表示档位本来是永久放行，
    一旦有规则就改为「到本时段结束」，不会变成永久。
    """
    ts = time.time() if ts is None else float(ts)
    st = window_state(sched, ts)
    if st["unlimited"]:
        return int(expire), None
    if not st["allowed"]:
        raise OutsideSchedule(hint(st))
    end = int(st["ends_at"])
    if expire == 0 or expire > end:
        # 至少给 1 秒，避免 expire<=now 被误当成「永久放行」
        return max(end, int(ts) + 1), st["ends_hhmm"]
    return int(expire), None
