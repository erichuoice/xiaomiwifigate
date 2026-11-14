#!/usr/bin/env python3
"""设备答题时段：每台设备可按周一~周日配置多个「可以答题放行」的时间段。

两种时段（3.17.0 起每天都可以各配多个）：
  · 答题时段            —— 可以答题，放行时长最多到本时段结束
  · 自由上网时段（免答题）—— 到点自动放行，不需要答题；时段结束自动恢复限制
  · 两种时段之外         —— 不能开始答题，已有放行会被自动回收
  · 家长在后台手动点「放行」属于临时破例，不受时段限制

自由上网时段的写法是在时间段前加 `free:`，例如 `free:18:00-19:00`；
两种时段可以同一天混着写，也可以重叠（重叠时按「自由上网」算）。

时间按服务所在时区解释（容器已设 TZ=Asia/Shanghai），
格式 HH:MM，支持 22:00-06:00 这种跨午夜时段，00:00-24:00 表示全天。
留空的星期 = 当天不允许答题；整周都留空 = 不限制（等同没配规则）。
"""

import re
import time

WEEK_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DAY_MINUTES = 24 * 60
MAX_WINDOWS_PER_DAY = 12

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


FREE_PREFIX = ("free:", "free：", "免:", "免：", "免答题:")


def parse_window(raw):
    """把一个时间段规范化成 ["开始","结束"]；start>end 表示跨午夜。

    3.17.0 起支持免答题的「自由上网时段」：在时间段前加 `free:`，
    返回 ["开始","结束","free"]；普通答题时段仍返回两个元素。
    """
    kind = ""
    if isinstance(raw, str):
        text = raw.strip()
        low = text.lower()
        for prefix in FREE_PREFIX:
            if low.startswith(prefix):
                kind = "free"
                text = text[len(prefix):].strip()
                break
        m = _RANGE_RE.match(text)
        if not m:
            raise ScheduleError("时间段要写成 18:00-20:00（免答题写 free:18:00-20:00）：%s"
                                % raw)
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
    out = [_fmt(s), _fmt(e)]
    if kind:
        out.append(kind)
    return out


def window_kind(w):
    """[start, end] 或 [start, end, "free"] → "quiz" / "free"。"""
    return "free" if isinstance(w, (list, tuple)) and len(w) > 2 and w[2] == "free" \
        else "quiz"


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
        seen = set()
        for item in _day_items(raw):
            w = parse_window(item)
            key = (w[0], w[1], window_kind(w))
            if key not in seen:
                seen.add(key)
                windows.append(w)
        if len(windows) > MAX_WINDOWS_PER_DAY:
            raise ScheduleError("%s 最多设 %s 个时间段（答题 + 免答题合计）" %
                                (WEEK_NAMES[i], MAX_WINDOWS_PER_DAY))
        windows.sort(key=lambda w: (_minutes(w[0]), 0 if window_kind(w) == "free" else 1))
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
        "free": False, "free_until": None, "free_start": None,
        "free_hhmm": None,
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
    active = []                                 # 当前生效的时段 (begin, end, kind)
    starts = []
    # 跨午夜窗口（如昨天 22:00-06:00）会延续到今天凌晨
    prev_index = (wday - 1) % 7
    for w in (days[prev_index] if prev_index < len(days) else []):
        s, e = _minutes(w[0]), _minutes(w[1], allow_24=True)
        if s > e and minute < e:
            begin = midnight - 86400 + s * 60
            active.append((begin, midnight + e * 60, window_kind(w)))
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
                    active.append((begin, base + e * 60, window_kind(w)))
            elif minute >= s:                    # 跨午夜：当晚开始，次日结束
                active.append((begin, base + 86400 + e * 60, window_kind(w)))

    allowed = bool(active)
    free_wins = [a for a in active if a[2] == "free"]
    free = bool(free_wins)
    ends_at = max(a[1] for a in active) if active else None
    # 下次状态真正翻转的时刻：最早一个「结束后没有别的时段接着」的点
    change_at = min(starts) if starts else None
    if active:
        for e in sorted({a[1] for a in active}):
            if not any(b < e < en for b, en, _ in active):
                change_at = e
                break
    next_start = min(starts) if starts else None
    today_wins = days[wday] if wday < len(days) else []
    return {
        "enabled": True, "limited": True,
        "allowed": allowed, "unlimited": False,
        "free": free,
        "free_until": max(a[1] for a in free_wins) if free_wins else None,
        "free_start": min(a[0] for a in free_wins) if free_wins else None,
        "free_hhmm": (time.strftime("%H:%M", time.localtime(
            max(a[1] for a in free_wins))) if free_wins else None),
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
        "today_text": "、".join(_win_text(w) for w in today_wins)
                      or "今天不开放",
        "summary": summary(sched),
    }


def _win_text(w):
    """给页面看的单个时段文字：免答题的时段加「（免答题）」。"""
    tail = "（免答题）" if window_kind(w) == "free" else ""
    return "%s-%s%s" % (w[0], w[1], tail)


def summary(sched):
    """一周规则的可读摘要，尽量合并相同的天。"""
    if not limited(sched):
        return "未设置（随时可答题）"
    days = sched["days"]
    parts = []
    for i in range(7):
        wins = days[i] if i < len(days) else []
        parts.append("、".join(_win_text(w) for w in wins))
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
    if st.get("free"):
        return "现在是免答题自由上网时段，到 %s 结束（不用答题）" % st["free_hhmm"]
    if st.get("allowed"):
        return "当前可答题，本时段到 %s 结束" % st["ends_hhmm"]
    if st.get("next_label"):
        return "现在不在答题时间（%s）。下次可答题：%s" % (
            st.get("today_text"), st["next_label"])
    return "现在不在答题时间（%s）" % st.get("today_text")


def clamp_expire(expire, sched, ts=None):
    """把放行到期时间夹在本时段结束之前；时段外拒绝放行。

    自由上网时段（免答题）同样算「可放行」，放行时长也截到时段结束。

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
