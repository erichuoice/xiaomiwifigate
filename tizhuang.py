#!/usr/bin/env python3
"""题庄在线题库（tizhuang.qcscience.cc）客户端。

用途：按「学生地区 → 教材版本 → 年级学期 → 已学章节」从题庄真题库抽题，
导进我们自己的题库；同时把「已学章节」交给 AI 出题，避免出到还没学的知识点。

额度：未注册试用 50 题 / 24 小时（每个匿名 client 一个 token，缓存在
state/tizhuang.json）；注册后每天 150 题起。元数据（学科/年级/教材/章节/知识点）
不扣额度。拿到注册 License 后可以：
  - 设置环境变量 QUESTION_SERVICE_LICENSE=...
  - 或在后台「系统设置 → 题庄题库」里粘贴（存 state/tizhuang.json，权限 600）
注意：题庄题目内容归题庄所有，仅供自家孩子练习使用，不要公开再分发。
"""

import calendar
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("QUESTION_SERVICE_URL", "https://tizhuang.qcscience.cc/api")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "state", "tizhuang.json")
CACHE_TTL = 6 * 3600        # 元数据缓存 6 小时
TERM_WEEKS = 20             # 一学期按 20 教学周估算（只看章节顺序，不追求精确）

_mem = {}                   # {(path, params): (expire_ts, data)}


class TzError(Exception):
    pass


# ---------------------------------------------------------------- 状态文件

def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def license_key():
    env = (os.environ.get("QUESTION_SERVICE_LICENSE") or "").strip()
    if env:
        return env
    return str(_load_state().get("license") or "").strip()


def set_license(value):
    """保存/清除注册 License（空字符串 = 清除，回到匿名试用）。"""
    st = _load_state()
    value = str(value or "").strip()
    if value:
        st["license"] = value
    else:
        st.pop("license", None)
        st.pop("trial_token", None)
        st.pop("expires_at", None)
    _save_state(st)
    _mem.clear()
    return bool(value)


def config_view():
    st = _load_state()
    key = license_key()
    return {
        "has_license": bool(key),
        "key_hint": ("…" + key[-4:]) if key else "",
        "base_url": BASE_URL,
        "trial_expires_at": st.get("expires_at") or "",
    }


# ---------------------------------------------------------------- HTTP

def _http(path, params=None, method="GET", headers=None, body=None, timeout=30):
    url = BASE_URL.rstrip("/") + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail") or ""
        except Exception:
            pass
        raise TzError("题庄接口 %s 失败（HTTP %s）%s" % (path, exc.code,
                                                      ("：" + str(detail)) if detail else ""))
    except Exception as exc:                       # 网络/超时/JSON
        raise TzError("连接题庄失败：%s" % exc)


def _auth_headers():
    key = license_key()
    if key:
        return {"X-API-Key": key}
    return {"X-Trial-Token": _trial_token()}


def _trial_token():
    st = _load_state()
    exp = str(st.get("expires_at") or "")
    if st.get("trial_token") and exp:
        try:
            if _parse_time(exp) > time.time() + 120:
                return st["trial_token"]
        except ValueError:
            pass
    client_id = st.get("client_id") or secrets.token_urlsafe(24)
    res = _http("/v1/trials", method="POST",
                headers={"X-Anonymous-Client-ID": client_id}, body={})
    token = str(res.get("trial_token") or "")
    if not token:
        raise TzError("题庄没有返回试用 token")
    st.update({"client_id": client_id, "trial_token": token,
               "expires_at": res.get("expires_at") or "",
               "base_url": BASE_URL})
    _save_state(st)
    return token


def _parse_time(text):
    # 题庄返回 "2026-11-15T16:14:06"（UTC，无时区标记）
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", str(text))
    if not m:
        raise ValueError(text)
    y, mo, d, h, mi, se = (int(x) for x in m.groups())
    return calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))


def _meta(path, params=None):
    """免费元数据：/v1/meta/*（不扣额度），内存缓存 6 小时。"""
    key = (path, tuple(sorted((params or {}).items())))
    hit = _mem.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    data = _http(path, params, headers=_auth_headers())
    _mem[key] = (now + CACHE_TTL, data)
    return data


def _api(path, params=None):
    """计量接口：试用走 /v1/trial/*，注册 License 走 /v1/*。"""
    if license_key():
        return _http(path, params, headers=_auth_headers())
    trial_path = path
    if path.startswith("/v1/") and not path.startswith("/v1/trial/") and \
            path not in ("/v1/trials",):
        trial_path = "/v1/trial" + path[3:]
    return _http(trial_path, params, headers=_auth_headers())


# ---------------------------------------------------------------- 元数据

def subjects():
    return _meta("/v1/meta/subjects")


def grades():
    return _meta("/v1/meta/grades")


def phases():
    return _meta("/v1/meta/phases")


def question_types(subject_id, phase_id):
    return _meta("/v1/meta/question-types",
                 {"subject_id": subject_id, "phase_id": phase_id})


def editions(subject_id, grade_id):
    return _meta("/v1/meta/editions",
                 {"subject_id": subject_id, "grade_id": grade_id})


def chapters(subject_id, grade_id, edition_id):
    return _meta("/v1/meta/chapters",
                 {"subject_id": subject_id, "grade_id": grade_id,
                  "edition_id": edition_id})


def knowledge_points(subject_id, phase_id, keyword=None, limit=200):
    return _meta("/v1/meta/knowledge-points",
                 {"subject_id": subject_id, "phase_id": phase_id,
                  "keyword": keyword, "limit": limit})


def quota():
    res = _api("/v1/quota")
    if isinstance(res, dict):
        remaining = res.get("remaining")
        if remaining is None:
            remaining = res.get("quota_remaining_today")
        return {"limit": res.get("question_limit") or res.get("daily_limit"),
                "used": res.get("used") or res.get("quota_used_today"),
                "remaining": remaining,
                "expires_at": res.get("expires_at") or "",
                "registered": bool(license_key())}
    return {}


# ---------------------------------------------------------------- 地区 / 学期 / 章节

# 地区 → 各科常用教材（按优先级；题庄里的教材名）。广州语文用统编（人教部编），
# 数学用人教版，英语用教科版（广州）。
REGION_SUBJECT_EDITIONS = {
    "guangdong": {
        1: ["人教部编2024", "部编版", "人教版（新课程标准）"],
        2: ["人教新版", "人教版2024", "人教版"],
        3: ["教科版（广州）", "教科版2024", "粤教沪外版2024", "人教版(PEP)"],
        10: ["教科版", "粤教版", "人教新版"],
    },
    "shanghai": {
        1: ["部编版", "人教部编2024"],
        2: ["沪教新版", "沪教版"],
        3: ["牛津上海版（试用本）", "新版-牛津上海版（深圳用）", "沪教(牛津深圳)版2024"],
        10: ["沪教新版", "沪教版"],
    },
    "beijing": {
        1: ["人教部编2024", "部编版"],
        2: ["北京新版", "北京版2024", "北京版"],
        3: ["北京版", "北京版2024"],
        10: ["北京版"],
    },
    "jiangsu": {
        1: ["部编版", "人教部编2024"],
        2: ["苏教新版", "苏教版", "苏教版2024"],
        3: ["牛津译林版(三起)2024", "新版-牛津译林版"],
        10: ["苏教版"],
    },
    "zhejiang": {
        1: ["部编版", "人教部编2024"],
        2: ["浙教版", "浙教新版"],
        3: ["人教版(PEP)", "人教版(三起pep)2024"],
        10: ["浙教版"],
    },
}
DEFAULT_SUBJECT_EDITIONS = {
    1: ["部编版", "人教部编2024", "人教版（新课程标准）"],
    2: ["人教新版", "人教版2024", "人教版"],
    3: ["人教版(PEP)", "人教版(三起pep)2024", "人教大同版2024"],
    10: ["教科版", "人教新版"],
}
STAGE_PHASE = {"小学": 1, "初中": 2, "高中": 3}


def region_key(region):
    text = str(region or "")
    for key, words in (("guangdong", ("广东", "广州", "深圳", "佛山", "东莞", "珠海", "中山")),
                       ("shanghai", ("上海",)), ("beijing", ("北京",)),
                       ("jiangsu", ("江苏",)), ("zhejiang", ("浙江",))):
        if any(w in text for w in words):
            return key
    return ""


def region_preferred_editions(region, subject_id=2):
    key = region_key(region)
    if key:
        return REGION_SUBJECT_EDITIONS[key].get(subject_id,
                                                DEFAULT_SUBJECT_EDITIONS.get(subject_id, []))
    return DEFAULT_SUBJECT_EDITIONS.get(subject_id, [])


def semester_of(progress):
    """从教学进度推断「上/下」：第一学期（秋季）→上，第二学期（春季）→下。"""
    if not isinstance(progress, dict):
        return "上"
    term = str(progress.get("term") or "")
    if "第二学期" in term or "春季" in term:
        return "下"
    vacation = str(progress.get("vacation") or "")
    if vacation == "summer":        # 暑假 = 刚读完下学期
        return "下"
    return "上"


def grade_row(grade_text, semester="上"):
    """把「三年级」+ 上/下 转成题庄年级 id（131 = 三年级上）。"""
    name = str(grade_text or "").strip()
    if not name:
        return None
    rows = grades()
    target = name + ("上" if semester == "上" else "下")
    for row in rows:
        if row.get("name") == target:
            return row
    for row in rows:
        if row.get("name") == name:
            return row
    return None


def pick_edition(region, subject_id, grade_id):
    rows = editions(subject_id, grade_id)
    if not rows:
        return None
    by_name = {r.get("name"): r for r in rows}
    for want in region_preferred_editions(region, subject_id):
        if want in by_name:
            return by_name[want]
    # 地区教材这个年级没有时，按科目兜底到通用版本
    for want in DEFAULT_SUBJECT_EDITIONS.get(subject_id, []):
        if want in by_name:
            return by_name[want]
    return rows[0]


def _chapter_order(chapter):
    m = re.match(r"第\s*(\d+)\s*[章课单元]", str(chapter.get("name") or ""))
    return int(m.group(1)) if m else 999


def chapter_plan(subject_id, term_grade_id, edition_id):
    """该教材该学期（上/下）的章节，按教材顺序排列。"""
    rows = chapters(subject_id, term_grade_id, edition_id)
    picked = [c for c in rows
              if c.get("grade_id") == term_grade_id and int(c.get("level") or 0) == 0]
    if not picked:                      # 有的科目没有分上下册 id，退回整个年级
        picked = [c for c in rows if int(c.get("level") or 0) == 0]
    picked.sort(key=_chapter_order)
    return picked


def learned_chapters(plan, week, term_weeks=TERM_WEEKS):
    """按当前教学周估算「已经学过的章节」：章节顺序 × 学期进度。"""
    if not plan:
        return []
    try:
        week = int(week or 0)
    except (TypeError, ValueError):
        week = 0
    week = max(0, min(term_weeks, week))
    if week >= term_weeks:
        n = len(plan)
    else:
        n = int(round(len(plan) * week / float(term_weeks) + 0.4999))
    return plan[:max(1, min(len(plan), n))]


def scope_for_bank(bank, progress=None):
    """一台（套）题库的「已学范围」：地区 → 教材 → 年级学期 → 已学章节。

    返回 {region, stage, grade, semester, week, subjects: [ ... ]}；
    网络失败时抛 TzError，让调用方回退。
    """
    bank = bank or {}
    progress = progress if isinstance(progress, dict) else {}
    region = str(bank.get("region") or "").strip()
    stage = str(bank.get("stage") or "").strip() or "小学"
    grade = str(bank.get("grade") or "").strip()
    semester = semester_of(progress)
    try:
        week = int(progress.get("week") or 0)
    except (TypeError, ValueError):
        week = 0
    phase_id = STAGE_PHASE.get(stage, 1)
    out = {"region": region, "stage": stage, "grade": grade,
           "semester": semester, "week": week, "phase_id": phase_id,
           "subjects": [], "errors": []}
    row = grade_row(grade, semester)
    if not row:
        out["errors"].append("没有设置年级，先到「学生信息」里填年级")
        return out
    out["grade_id"] = row["id"]
    out["grade_name"] = re.sub(r"[上下]$", "", str(row["name"]))
    for subj in subjects():
        sid = subj["id"]
        if sid not in (1, 2, 3, 10):    # 语文 / 数学 / 英语 / 科学
            continue
        try:
            ed = pick_edition(region, sid, row["id"])
            if not ed:
                continue
            plan = chapter_plan(sid, row["id"], ed["id"])
            learned = learned_chapters(plan, week)
            out["subjects"].append({
                "subject_id": sid, "subject": subj["name"],
                "edition_id": ed["id"], "edition": ed["name"],
                "chapters": [{"id": c["id"], "name": c["name"]} for c in plan],
                "learned": [{"id": c["id"], "name": c["name"]} for c in learned],
            })
        except TzError as exc:
            out["errors"].append("%s：%s" % (subj["name"], exc))
    return out


def scope_text(scope):
    """给后台显示的一句话（不依赖网络）。"""
    if not scope:
        return ""
    names = []
    for s in scope.get("subjects") or []:
        names.append("%s：%s" % (s["subject"],
                                 "、".join(c["name"] for c in s["learned"]) or "—"))
    return ("%s%s（%s）· 第 %s 教学周 · 已学章节：%s" % (
        scope.get("grade") or "", scope.get("semester") or "",
        "/".join(sorted({s["edition"] for s in scope.get("subjects") or []})) or "—",
        scope.get("week") or 0, "；".join(names) or "—"))


# ---------------------------------------------------------------- 抽题 / 转换

TYPES = {"选择题", "判断题", "多选题"}


def _type_names(subject_id, phase_id):
    """该科目该学段支持的题型名（题庄按名字过滤，按 id 会查不到）。"""
    names = [row.get("name") for row in question_types(subject_id, phase_id)]
    return [n for n in ("选择题", "判断题", "多选题") if n in names]


def _strip(text):
    text = str(text or "")
    text = re.sub(r"<input[^>]*>", "____", text, flags=re.I)
    text = re.sub(r"<img[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_topic(text):
    """题庄的 knowledges 类似「小学语文知识点》阅读理解###」，取最后一段做知识点。"""
    part = str(text or "").split("###")[0].strip()
    if "》" in part:
        part = [x for x in part.split("》") if x.strip()][-1].strip()
    return re.sub(r"^[#\s]+", "", part)[:24]


def convert_question(item, subject_name, stage, grade_name):
    """题庄题目 → 我们的题目格式（单选/多选/判断）。不支持的题型返回 None。"""
    qtype = str(item.get("question_type") or "")
    text = _strip(item.get("title_html") or item.get("title"))
    if not text or len(text) < 4:
        return None
    options = item.get("options") or {}
    answer = str(item.get("answer") or "").strip()
    explain = _strip(item.get("analysis_html") or item.get("analysis"))
    topic = _clean_topic(str(item.get("knowledges") or ""))
    try:
        star = int(round(float(item.get("difficulty") or 2)))
    except (TypeError, ValueError):
        star = 2
    star = max(1, min(5, star))
    base = {
        "id": "tz" + str(item.get("id") or ""),
        "stage": stage, "grade": grade_name, "subject": subject_name,
        "text": text[:400], "explain": explain[:400],
        "star": star, "topic": topic, "source": "题庄",
    }
    if qtype == "判断题":
        low = answer.lower().replace(" ", "")
        if low in ("√", "对", "正确", "t", "true", "是"):
            idx = 0
        elif low in ("×", "x", "错", "错误", "f", "false", "否"):
            idx = 1
        else:
            return None
        base.update({"type": "judge", "options": ["正确", "错误"], "answer": idx})
        return base
    if qtype in ("选择题", "多选题"):
        if not isinstance(options, dict) or len(options) < 2:
            return None
        keys = sorted(options.keys())
        labels = [_strip(options[k]) for k in keys]
        if any(not x for x in labels) or len(set(labels)) != len(labels):
            return None
        if qtype == "多选题":
            letters = [c for c in answer.upper() if c in keys]
            if len(letters) < 2:
                return None
            base.update({"type": "multi", "options": labels,
                         "answer": sorted(keys.index(c) for c in letters)})
            return base
        if answer.upper() not in keys:
            return None
        base.update({"type": "single", "options": labels,
                     "answer": keys.index(answer.upper())})
        return base
    return None


def pull_questions(bank, progress=None, count=10, subject=None, want_types=None):
    """按已学章节抽题（会扣题庄额度）。返回 {questions, scope, quota, skipped}。"""
    scope = scope_for_bank(bank, progress)
    if not scope.get("subjects"):
        raise TzError("；".join(scope.get("errors") or ["没有可用的学科数据"]))
    count = max(1, min(30, int(count or 10)))
    want_types = want_types or []
    q = quota()
    remaining = q.get("remaining")
    questions, used_ids = [], set()
    per_chapter = max(1, min(5, (count + 1) // 2))
    for subj in scope["subjects"]:
        if len(questions) >= count:
            break
        if subject and subj["subject"] != subject:
            continue
        available = _type_names(subj["subject_id"], scope["phase_id"])
        chosen = [n for n in want_types if n in available] or \
            [n for n in ("选择题", "判断题") if n in available]
        if not chosen:                      # 有的科目题型名不一样，退回通用题型名
            chosen = ["选择题", "判断题"]
        before = len(questions)
        for chap in subj["learned"]:
            if len(questions) >= count:
                break
            for tname in chosen:
                if len(questions) >= count:
                    break
                if remaining is not None and remaining <= 0:
                    break
                try:
                    res = _api("/v1/questions", {
                        "chapter_id": chap["id"], "question_type": tname,
                        "has_images": "false", "limit": per_chapter})
                except TzError:
                    # 题庄个别章节 + 题型组合会 500，跳过这个组合继续
                    continue
                items = (res or {}).get("items") or []
                if res and res.get("quota_remaining_today") is not None:
                    remaining = int(res["quota_remaining_today"])
                for item in items:
                    conv = convert_question(item, subj["subject"], scope["stage"],
                                            scope.get("grade_name") or scope["grade"])
                    if not conv or conv["text"] in used_ids:
                        continue
                    used_ids.add(conv["text"])
                    questions.append(conv)
                    if len(questions) >= count:
                        break
        if len(questions) == before and len(questions) < count and                 (remaining is None or remaining > 0):
            # 有的科目（语文 / 广州版英语）按章节抽不到无图选择题，
            # 退一步只按「年级 + 题型」抽（仍限定在上学期，不出下学期内容）
            for tname in chosen:
                if len(questions) >= count or (remaining is not None and remaining <= 0):
                    break
                try:
                    res = _api("/v1/questions", {
                        "subject_id": subj["subject_id"],
                        "grade_id": scope.get("grade_id"),
                        "question_type": tname, "has_images": "false",
                        "limit": max(1, count - len(questions)),
                        "random_order": "true"})
                except TzError:
                    continue
                items = (res or {}).get("items") or []
                if res and res.get("quota_remaining_today") is not None:
                    remaining = int(res["quota_remaining_today"])
                for item in items:
                    conv = convert_question(item, subj["subject"], scope["stage"],
                                            scope.get("grade_name") or scope["grade"])
                    if not conv or conv["text"] in used_ids:
                        continue
                    used_ids.add(conv["text"])
                    questions.append(conv)
                    if len(questions) >= count:
                        break
    if not questions:
        raise TzError("没有抽到符合「已学章节 + 无图 + 选择题/判断题」的题目，"
                      "可以稍后再试或换科目")
    return {"questions": questions, "scope": scope,
            "quota": quota() if remaining is not None else q}
