#!/usr/bin/env python3
"""题库导入解析：CSV / TSV / JSON 文本 → 题目列表。

设计目标：家长在 Excel 里填一张表就能导入，不需要懂 JSON。
- 表头支持中英文别名，列顺序随意
- 答案支持 A / a / A,C / AC / 1,3 / 对 / 错 / √ / × / 正确 / 错误
- 逐行校验，出错的行带行号和原因，其余行照常导入
"""

import csv
import io
import json
import re

import store

HEADER_ALIAS = {
    "id": "id", "编号": "id", "题号": "id",
    "type": "type", "题型": "type", "类型": "type",
    "stage": "stage", "学段": "stage", "阶段": "stage",
    "grade": "grade", "年级": "grade",
    "subject": "subject", "科目": "subject", "学科": "subject",
    "text": "text", "题干": "text", "题目": "text", "问题": "text",
    "question": "text", "内容": "text",
    "answer": "answer", "答案": "answer", "正确答案": "answer",
    "explain": "explain", "解析": "explain", "解释": "explain",
    "分析": "explain", "点评": "explain",
    "star": "star", "难度": "star", "星级": "star", "难度星级": "star",
    "options": "options", "选项": "options", "选项列表": "options",
}

TRUE_WORDS = {"对", "正确", "√", "t", "true", "y", "yes", "是"}
FALSE_WORDS = {"错", "错误", "×", "x", "f", "false", "n", "no", "否"}
LETTERS = "ABCDEFGH"
TYPE_CN = {"single": "单选", "multi": "多选", "judge": "判断"}


def _safe(values, idx):
    if idx is None or idx < 0 or idx >= len(values):
        return ""
    v = values[idx]
    return "" if v is None else str(v).strip()


def _map_header(fields):
    """原始表头 → ({标准字段: 列号}, [(列号, 字母)])"""
    mapping, option_cols = {}, []
    for idx, name in enumerate(fields):
        raw = (name or "").strip()
        if not raw:
            continue
        key = HEADER_ALIAS.get(raw.lower()) or HEADER_ALIAS.get(raw)
        if key:
            mapping.setdefault(key, idx)
            continue
        m = re.match(r"^(?:选项|option|opt)?\s*([A-Ha-h])$", raw)
        if m:
            option_cols.append((idx, m.group(1).upper()))
            continue
        m = re.match(r"^(?:选项|option|opt)\s*([1-8])$", raw)
        if m:
            option_cols.append((idx, LETTERS[int(m.group(1)) - 1]))
    option_cols.sort(key=lambda x: LETTERS.index(x[1]))
    return mapping, option_cols


def _parse_answer(raw, qtype, n_options):
    """答案文本 → int（单选/判断）或升序 int 列表（多选）。"""
    s = str(raw or "").strip()
    if n_options == 0:
        raise store.StoreError("没有填写选项")
    if not s:
        raise store.StoreError("缺少答案")
    low = s.lower().replace(" ", "")

    if qtype == "judge":
        if low in TRUE_WORDS:
            return 0
        if low in FALSE_WORDS:
            return 1

    if re.fullmatch(r"[A-Ha-h,，、;；/\s]+", s):
        idx = sorted({LETTERS.index(c.upper()) for c in re.findall(r"[A-Ha-h]", s)})
        if not idx:
            raise store.StoreError("无法识别答案「%s」" % s)
        if any(i >= n_options for i in idx):
            raise store.StoreError("答案 %s 超出选项范围（共 %s 个选项）" % (s, n_options))
        return idx if qtype == "multi" else idx[0]

    nums = re.findall(r"\d+", s)
    if nums:
        idx = [int(n) for n in nums]
        if qtype == "multi":
            if any(i >= n_options for i in idx):
                raise store.StoreError("答案 %s 超出选项范围" % s)
            return sorted(set(idx))
        if int(nums[0]) >= n_options:
            raise store.StoreError("答案 %s 超出选项范围（共 %s 个选项）" % (s, n_options))
        return int(nums[0])
    raise store.StoreError("无法识别答案「%s」" % s)


def _row_to_question(values, option_cols, mapping, default_stage=""):
    q = {}
    for key, idx in mapping.items():
        if key in ("options", "star"):
            continue
        val = _safe(values, idx)
        if val:
            q[key] = val
    if "star" in mapping:
        q["star"] = _safe(values, mapping["star"]) or 2

    opts = []
    if option_cols:
        for idx, _letter in option_cols:
            opts.append(_safe(values, idx))
        while opts and not opts[-1]:
            opts.pop()
    elif "options" in mapping:
        raw = _safe(values, mapping["options"])
        opts = [p.strip() for p in re.split(r"[|｜/、]+", raw) if p.strip()]

    qtype = store.norm_type(q.get("type"))
    if qtype == "judge" and not opts:
        opts = ["正确", "错误"]
    q["options"] = opts
    q["answer"] = _parse_answer(q.get("answer"), qtype, len(opts))
    if not str(q.get("stage") or "").strip() and default_stage:
        q["stage"] = default_stage
    return q


def _guess_delimiter(text):
    """显式判定分隔符。

    不用 csv.Sniffer：它在只有一两行、或中文字段多的表格上经常退回逗号模式，
    导致整行被当成一列，所有行都解析失败。
    """
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    counts = {d: first.count(d) for d in ("\t", ",", ";")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def parse_csv(text, default_stage=""):
    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text), delimiter=_guess_delimiter(text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise store.StoreError("没有读到任何数据行")

    mapping, option_cols = _map_header(rows[0])
    has_header = "text" in mapping and "answer" in mapping
    start = 1 if has_header else 0

    if not has_header:
        # 无表头：按 题干 | 选项A~D | 答案 | 解析 的位置约定。
        # 列数取第一行，不能用最大宽度——后面某行多一列会把答案列整体错位。
        width = len(rows[0])
        n_opt = max(2, min(6, width - 2))
        option_cols = [(1 + i, LETTERS[i]) for i in range(n_opt)]
        mapping = {"text": 0, "answer": 1 + n_opt, "explain": 2 + n_opt}

    questions, errors = [], []
    for offset, row in enumerate(rows[start:], start + 1):
        if not any((c or "").strip() for c in row):
            continue
        try:
            q = _row_to_question(row, option_cols, mapping, default_stage)
            questions.append(store.validate_question(q, offset))
        except store.StoreError as exc:
            errors.append({"row": offset, "msg": str(exc),
                           "raw": " | ".join(c for c in row if c)[:120]})
    return questions, errors


def parse_json(text, default_stage=""):
    text = text.lstrip("\ufeff").strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise store.StoreError("JSON 解析失败：%s" % exc)
    if isinstance(data, dict):
        if isinstance(data.get("quiz"), dict):
            data = data["quiz"].get("questions", [])
        elif "questions" in data:
            data = data["questions"]
    if not isinstance(data, list):
        raise store.StoreError("JSON 里没有找到题目数组")

    questions, errors = [], []
    for i, item in enumerate(data, 1):
        try:
            if not isinstance(item, dict):
                raise store.StoreError("第 %s 条不是对象" % i)
            if default_stage and not item.get("stage"):
                item = dict(item, stage=default_stage)
            questions.append(store.validate_question(item, i))
        except store.StoreError as exc:
            errors.append({"row": i, "msg": str(exc),
                           "raw": json.dumps(item, ensure_ascii=False)[:120]})
    return questions, errors


def parse_text(text, fmt="auto", default_stage=""):
    text = (text or "").strip()
    if not text:
        raise store.StoreError("导入内容为空")
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = "json" if text[:1] in "[{" else "csv"
    questions, errors = (parse_json if fmt == "json" else parse_csv)(
        text, default_stage)
    return {"format": fmt, "questions": questions, "errors": errors}


TEMPLATE_HEADER = ["题型", "学段", "年级", "科目", "题干",
                   "选项A", "选项B", "选项C", "选项D", "答案", "解析", "难度"]
TEMPLATE_ROWS = [
    ["单选", "小学", "三年级", "数学", "一个正方形的边长是 6 厘米，周长是多少？",
     "12 厘米", "24 厘米", "36 厘米", "30 厘米", "B", "周长 = 边长 × 4 = 24 厘米。", "2"],
    ["判断", "小学", "四年级", "科学", "月亮自己能发光。",
     "正确", "错误", "", "", "错", "月亮反射太阳光，本身不发光。", "1"],
    ["多选", "初中", "八年级", "数学", "下面哪些是偶数？",
     "2", "7", "10", "15", "A,C", "个位是 0、2、4、6、8 的是偶数。", "3"],
    ["单选", "初中", "九年级", "物理", "水的沸点（标准大气压）是多少？",
     "90℃", "100℃", "110℃", "120℃", "B", "标准大气压下水的沸点是 100℃。", "2"],
]


def template_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_HEADER)
    for row in TEMPLATE_ROWS:
        w.writerow(row)
    return "\ufeff" + buf.getvalue()


def bank_to_csv(questions):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_HEADER + ["ID"])
    for q in questions:
        opts = list(q["options"]) + [""] * 4
        ans = q["answer"]
        ans_text = (",".join(LETTERS[i] for i in ans)
                    if isinstance(ans, list) else LETTERS[ans])
        w.writerow([TYPE_CN.get(q["type"], "单选"), q.get("stage", ""),
                    q.get("grade", ""), q.get("subject", ""), q["text"],
                    opts[0], opts[1], opts[2], opts[3],
                    ans_text, q.get("explain", ""), q.get("star", 2), q["id"]])
    return "\ufeff" + buf.getvalue()
