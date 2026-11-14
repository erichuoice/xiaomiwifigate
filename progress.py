#!/usr/bin/env python3
"""广州中小学「当前学习进度」推断（3.6.0）。

用途：AI 出题时把「现在是哪个学期、大约学到哪里」写进 prompt，
避免 11 月给孩子出到下学期的超纲题。

设计原则（重要）：
  只按校历做**粗略**推断，不声称知道任何学校/教材的精确单元进度。
  推断结果在后台可见、可一键关闭、可用「教学进度范围」手动覆盖，
  手动填写的内容优先级最高。

零第三方依赖，可独立单测。
"""

import datetime as _dt
import re

REGION = "广东省广州市"

# 广州中小学常用教材版本（仅作 prompt 参考，不声称精确对应某所学校）
TEXTBOOKS = [
    {"id": "gz", "label": "广州常用（语文统编版 / 数学人教版 / 英语教科版广州版）",
     "value": "广州地区常用教材（语文统编版 / 数学人教版 / 英语教科版广州版）"},
    {"id": "renjiao", "label": "人教版（全科目）", "value": "人教版（人民教育出版社）"},
    {"id": "tongbian", "label": "统编版 / 部编版", "value": "统编版（部编版）教材"},
    {"id": "beishi", "label": "北师大版（数学）", "value": "北京师范大学出版社教材"},
    {"id": "hujiao", "label": "沪教版（英语，广州部分区）", "value": "上海教育出版社（沪教版）教材"},
    {"id": "yuejiao", "label": "粤教版 / 教科版（科学等）", "value": "粤教版 / 教育科学出版社教材"},
]

DEFAULT_TEXTBOOK = TEXTBOOKS[0]["value"]

# 学期大致长度（教学周），只用于把日期换算成「进度阶段」
TERM_WEEKS = 20
AUTUMN_START = (9, 1)       # 秋春季开学日按广州校历惯例估算
SPRING_START = (2, 20)


def _phase(week, vacation):
    if vacation == "summer":
        return "暑假（复习与预习）"
    if vacation == "winter":
        return "寒假 / 期末收尾"
    if week <= 4:
        return "开学初"
    if week <= 9:
        return "期中前"
    if week <= 12:
        return "期中前后"
    if week <= 18:
        return "期末复习前"
    return "期末复习与考试"


def _scope(phase, vacation, term_key):
    if vacation == "summer":
        return "刚结束学年的已学内容（用于巩固复习），不要引入新学年新知识"
    if vacation == "winter":
        return "本学期已学全部内容（期末复习范围），不要出现下学期内容"
    if phase == "开学初":
        return "本学期开头 1~2 个单元的内容，以基础概念与刚学过的知识为主"
    if phase == "期中前":
        return "本学期前半册截至目前已学内容（期中之前），不要出期中后的单元"
    if phase == "期中前后":
        return "本学期前半册已学内容（开学到期中复习范围），不出后半册新单元"
    if phase == "期末复习前":
        return "本学期截至目前已学内容（含期中后新授单元），不出行将结束前未学的内容"
    return "本学期已学全部内容（期末复习范围），可以综合，但不跨到下学期"


def current_progress(now=None, scope=""):
    """返回当前学期进度描述。

    now   : datetime / 时间戳 / None（用当天）
    scope : 后台手动填写的教学进度范围，非空时直接优先采用
    """
    if isinstance(now, (int, float)):
        d = _dt.datetime.fromtimestamp(float(now))
    elif isinstance(now, _dt.datetime):
        d = now
    else:
        d = _dt.datetime.now()
    year, month = d.year, d.month

    # 学年：9 月起算新学年；同年 1 月仍属上学年第一学期的期末
    if month >= 9:
        start_year = year
        term, term_key, vacation = "第一学期（秋季）", "autumn", ""
        autumn_start = _dt.datetime(start_year, *AUTUMN_START)
        week = int((d - autumn_start).days // 7) + 1
    elif month == 1 or (month == 2 and d.day < SPRING_START[1]):
        start_year = year - 1
        autumn_start = _dt.datetime(start_year, *AUTUMN_START)
        week = int((d - autumn_start).days // 7) + 1
        if month == 1 and d.day <= 20:
            term, term_key, vacation = "第一学期（秋季）", "autumn", ""
        else:
            term, term_key, vacation, week = "寒假", "winter", "winter", 0
    else:
        start_year = year - 1
        spring_start = _dt.datetime(year, *SPRING_START)
        if d < spring_start:
            term, term_key, vacation, week = "寒假", "winter", "winter", 0
        elif month in (7, 8):
            term, term_key, vacation, week = "暑假", "summer", "summer", 0
        else:
            term, term_key, vacation = "第二学期（春季）", "spring", ""
            week = int((d - spring_start).days // 7) + 1
    school_year = "%d–%d 学年" % (start_year, start_year + 1)

    week = max(0, min(TERM_WEEKS + 3, week))
    phase = _phase(week, vacation)
    auto_scope = _scope(phase, vacation, term_key)
    manual = str(scope or "").strip()[:80]
    week_text = ("约第 %d 教学周" % week) if week else "假期"
    label = "%s %s · %s（%s）" % (school_year, term, week_text, phase)

    return {
        "school_year": school_year,
        "term": term, "term_key": term_key, "vacation": vacation,
        "week": week, "week_text": week_text, "phase": phase,
        "scope": manual or auto_scope,
        "auto_scope": auto_scope,
        "manual_scope": manual,
        "label": label,
        "date": d.strftime("%Y-%m-%d"),
        "month": month,
        "note": ("按广州市中小学校历惯例（9 月 1 日 / 2 月下旬开学）粗略估算，"
                 "不代表孩子所在学校的精确进度；如有出入请在「教学进度范围」手动填写"),
        "region": REGION,
    }


def prompt_lines(spec=None, progress=None, scope=None):
    """给 AI 出题 prompt 用的进度约束段落（列表，每行一句）。"""
    spec = spec or {}
    p = dict(progress or current_progress())
    manual = str(scope if scope is not None else (spec.get("scope") or "")).strip()[:80]
    if manual:
        p["scope"] = manual
        p["manual_scope"] = manual
    textbook = str(spec.get("textbook") or DEFAULT_TEXTBOOK)
    grade = str(spec.get("grade") or "").strip()
    stage = str(spec.get("stage") or "").strip()
    lines = [
        "【教学进度｜最高优先级】",
        "今天是 %s，%s中小学处于 %s %s，%s（%s）。" % (
            p["date"], REGION, p["school_year"], p["term"],
            p["week_text"], p["phase"]),
        "命题范围只覆盖：%s%s。" % (
            ("%s%s的" % (stage, grade)) if grade else "", p["scope"]),
        "教材参考：%s；严格按该学段该年级课程标准命题，不超纲、不出尚未学到的知识点。" % textbook,
    ]
    if p["manual_scope"]:
        lines.append("注意：上面的范围是家长手动指定的，以它为准，忽略校历推断的默认范围。")
    else:
        lines.append("若与实际教学进度有出入，以家长在后台填写的范围为准；宁可偏基础，不要抢进度。")
    return lines


def resolve(spec=None, config=None):
    """合并 spec / 后台配置，产出 (progress, textbook, progress_lines)。"""
    spec = dict(spec or {})
    cfg = dict(config or {})
    auto = bool(cfg.get("progress_auto", True)) and not str(spec.get("scope") or "").strip()
    scope = "" if auto else str(cfg.get("term_scope") or "").strip()
    if str(spec.get("scope") or "").strip():
        scope = str(spec["scope"]).strip()
    p = current_progress(scope=scope)
    spec["textbook"] = str(spec.get("textbook") or cfg.get("textbook") or DEFAULT_TEXTBOOK)
    return p, spec["textbook"], prompt_lines(spec, p, scope)


def textbook_options():
    return [{"id": t["id"], "label": t["label"], "value": t["value"]}
            for t in TEXTBOOKS]


def clean_scope(value):
    """后台表单校验：只保留安全的中文/数字/标点，防止 prompt 注入过长内容。"""
    s = re.sub(r"[\r\n\t]", " ", str(value or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s[:80]
