#!/usr/bin/env python3
"""AI 题库出题：OpenAI 兼容接口 + 广州本地化命题 + 离线降级生成。

设计约束：
- API Key 只落 state/ai.json（.gitignore 已排除 state/），接口回显一律打码，
  绝不写进 config.json、日志、备份导出或 GitHub。
- 生成结果先过 store.validate_questions 校验，再与现有题干去重，才允许入库。
- 没配 Key / 接口失败时自动降级为离线模板生成器，保证「随机出题」始终可用。
- 命题对象固定为广东广州地区中小学，按课标、按年级、不超纲。
"""

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

import learning
import progress
import store

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
CONFIG_PATH = os.path.join(STATE, "ai.json")

LETTERS = "ABCDEFGH"
REGION = "广东省广州市"

PRESETS = [
    {
        "id": "deepseek", "label": "DeepSeek 官方（推荐）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "价格低、中文命题稳定；路由器实测可直连",
    },
    {
        "id": "qwen", "label": "阿里百炼 · 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "国内节点，路由器实测可直连",
    },
    {
        "id": "custom", "label": "自定义 OpenAI 兼容接口",
        "base_url": "", "model": "", "key_url": "",
        "note": "填到 /v1 这一级，例如 https://xxx/v1",
    },
]

DEFAULTS = {
    "provider": "deepseek",
    "base_url": PRESETS[0]["base_url"],
    "model": PRESETS[0]["model"],
    "api_key": "",
    "timeout": 90,
    "region": REGION,
    "textbook": progress.DEFAULT_TEXTBOOK,
    "progress_auto": True,      # 按当前日期推断学期与教学进度
    "term_scope": "",           # 手动指定范围（如「第 1—3 单元」），非空时优先
    "auto_offline": True,
    "auto_topup": 0,          # 题库新题不足时自动补充的题数，0 = 关闭
}

STAGES = ["小学", "初中", "高中"]
GRADES = ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
          "七年级", "八年级", "九年级"]
SUBJECTS = ["语文", "数学", "英语", "科学", "物理", "化学", "生物", "历史", "地理"]
TYPES = {"single": "单选题", "multi": "多选题", "judge": "判断题"}
# 一次最多混合几个科目：直接按科目表长度（语文/数学/英语/科学/物理/化学/生物/历史/地理），
# 3.14.4 起不再卡 6 科——之前选第 7 科会被静默截掉，看起来像「勾不上」。
MAX_SUBJECTS = len(SUBJECTS)


class AIError(Exception):
    pass


def normalize_subjects(value, default=("数学",)):
    """把「科目」收敛成列表（3.9.0 起支持多选）。

    兼容四种输入：列表 ``["数学","语文"]``、逗号/顿号/空格分隔的字符串
    ``"数学、语文"``、单字符串 ``"数学"``、空值（回落到 default）。
    去重保序，单项最多 16 字，最多 MAX_SUBJECTS 项。
    """
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[,，、;；/|\s]+", str(value or ""))
    out = []
    for part in parts:
        name = str(part).strip()[:16]
        if not name or name in out:
            continue
        out.append(name)
        if len(out) >= MAX_SUBJECTS:
            break
    if not out:
        out = [str(x).strip() for x in default if str(x).strip()] or ["数学"]
    return out


def spec_subjects(spec):
    """从出题参数里取科目列表：优先 subjects，回退旧的 subject 字段。"""
    spec = spec or {}
    raw = spec.get("subjects")
    if not raw:
        raw = spec.get("subject")
    return normalize_subjects(raw)


def allocate(count, n):
    """把 count 道题尽量平均分给 n 个科目，余数给前面的科目。"""
    count, n = max(0, int(count or 0)), max(1, int(n or 1))
    base, rem = divmod(count, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


# ---------------------------------------------------------------- 配置

def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
    cfg["timeout"] = min(180, max(10, int(cfg.get("timeout") or 90)))
    return cfg


def _write_config(cfg):
    os.makedirs(STATE, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def public_config():
    """给后台页面的配置：密钥只留后 4 位，永远不回传明文。"""
    cfg = load_config()
    key = str(cfg.get("api_key") or "")
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout": cfg["timeout"],
        "region": cfg["region"],
        "textbook": cfg["textbook"],
        "progress_auto": bool(cfg.get("progress_auto", True)),
        "term_scope": str(cfg.get("term_scope") or ""),
        "progress": progress.current_progress(
            scope="" if cfg.get("progress_auto", True) else cfg.get("term_scope")),
        "textbooks": progress.textbook_options(),
        "auto_offline": bool(cfg["auto_offline"]),
        "auto_topup": int(cfg["auto_topup"] or 0),
        "has_key": bool(key),
        "key_hint": ("sk-****%s" % key[-4:]) if len(key) > 4 else ("已配置" if key else ""),
        "presets": PRESETS,
        "stages": STAGES, "grades": GRADES, "subjects": SUBJECTS,
        "types": TYPES,
    }


def save_config(patch):
    """保存后台表单。api_key 留空 / 打码 / 不变时保留原密钥。"""
    if not isinstance(patch, dict):
        raise AIError("配置格式不正确")
    cfg = load_config()
    old_key = str(cfg.get("api_key") or "")
    provider = str(patch.get("provider") or cfg["provider"]).strip()
    preset = next((p for p in PRESETS if p["id"] == provider), None)
    if preset is None:
        raise AIError("未知的服务商")

    changed_provider = provider != cfg["provider"]
    cfg["provider"] = provider
    if preset["base_url"] and (changed_provider or not patch.get("base_url")):
        cfg["base_url"] = preset["base_url"]
    elif patch.get("base_url") is not None:
        cfg["base_url"] = str(patch["base_url"]).strip().rstrip("/")
    if preset["model"] and (changed_provider or not patch.get("model")):
        cfg["model"] = preset["model"]
    elif patch.get("model") is not None:
        cfg["model"] = str(patch["model"]).strip()

    key = str(patch.get("api_key") or "").strip()
    if key and "*" not in key and key != old_key:
        cfg["api_key"] = key
    if patch.get("clear_key"):
        cfg["api_key"] = ""

    url = str(cfg["base_url"] or "").strip()
    if url and not re.match(r"^https?://", url):
        raise AIError("接口地址必须以 http:// 或 https:// 开头")
    if not url:
        cfg["base_url"] = ""
    cfg["model"] = str(cfg["model"] or "").strip()
    cfg["timeout"] = min(180, max(10, int(patch.get("timeout") or cfg["timeout"])))
    cfg["region"] = REGION
    cfg["textbook"] = str(patch.get("textbook") or cfg["textbook"]).strip()[:120]
    cfg["progress_auto"] = bool(patch.get("progress_auto", cfg.get("progress_auto", True)))
    if "term_scope" in patch:
        cfg["term_scope"] = progress.clean_scope(patch.get("term_scope"))
    cfg["auto_offline"] = bool(patch.get("auto_offline", cfg["auto_offline"]))
    cfg["auto_topup"] = min(50, max(0, int(patch.get("auto_topup") or 0)))
    _write_config(cfg)
    return public_config()


# ---------------------------------------------------------------- 接口调用

def _endpoint(cfg):
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise AIError("还没填接口地址")
    if base.endswith("/chat/completions"):
        return base
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base + "/chat/completions"


def _chat(cfg, system, user, max_tokens=3600):
    key = str(cfg.get("api_key") or "")
    if not key:
        raise AIError("还没填写 API Key")
    url = _endpoint(cfg)
    body = {
        "model": cfg["model"] or "deepseek-chat",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.9,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + key,
                 "User-Agent": "wifi-gate/ai-bank"},
        method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=int(cfg["timeout"])) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:220]
        if exc.code == 401:
            raise AIError("API Key 无效或已过期（401）")
        if exc.code == 402 or "insufficient" in detail.lower():
            raise AIError("账户余额不足（402）")
        if exc.code == 429:
            raise AIError("接口限流或额度用尽（429），稍后再试")
        raise AIError("接口返回 HTTP %s：%s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise AIError("连不上接口：%s" % exc.reason)
    except Exception as exc:
        raise AIError("调用失败：%s" % type(exc).__name__)
    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("接口返回格式异常，不是标准 chat/completions 响应")
    return content, round(time.time() - started, 1)


def test_connection(cfg=None):
    cfg = cfg or load_config()
    content, latency = _chat(cfg, "你是连通性测试助手，只回复用户要求的文字。",
                             "只回复两个字：正常", max_tokens=16)
    return {"ok": True, "latency": latency, "model": cfg["model"],
            "reply": str(content).strip()[:40]}


# ---------------------------------------------------------------- Prompt

SYSTEM_PROMPT = (
    "你是广东省广州市中小学的资深命题老师，也是严格的 JSON 生成器。"
    "你只输出一个 JSON 数组，不输出 markdown 代码块、不加任何解释文字。"
)


def build_prompt(spec, mix, variant_text=""):
    stage = str(spec.get("stage") or "小学")
    grade = str(spec.get("grade") or "")
    subjects = spec_subjects(spec)
    subject = subjects[0]
    count = int(spec.get("count") or 10)
    topics = spec.get("topics") or []
    if isinstance(topics, str):
        topics = [t.strip() for t in re.split(r"[,，、;；\s]+", topics) if t.strip()]
    _, textbook, plines = progress.resolve(spec)
    region = REGION
    if len(subjects) == 1:
        head = "请为%s%s的%s学生出 %d 道题。" % (
            region, stage + (grade or ""), subject, count)
    else:
        alloc = allocate(count, len(subjects))
        head = "请为%s%s学生出 %d 道题，混合以下科目：%s（每科都要有题，可按难度微调）。" % (
            region, stage + (grade or ""), count,
            "、".join("%s %d 道" % (s, n) for s, n in zip(subjects, alloc) if n))
    multi = len(subjects) > 1
    reqs = [
        "严格依据《义务教育课程标准》与上述教材版本，不超纲、不出现该年级没学过的知识；"
        "语言难度符合该年级认知水平，使用简体中文（英语科目的题干与选项可用英文）。",
        "情境尽量自然融入广东广州本地元素（珠江、广州塔、越秀公园、陈家祠、沙面、"
        "白云山、海心沙、北京路、上下九、广州地铁、早茶点心、荔枝、醒狮、粤剧、龙舟、"
        "木棉花、南沙湿地等），但不得因情境引入超纲内容，也不要出现商业广告、"
        "真实人名隐私或政治敏感内容。",
        "题型严格按下列数量出题：单选题 %d 道（4 个选项、唯一正确答案）、"
        "多选题 %d 道（4~5 个选项、至少 2 个正确答案）、"
        "判断题 %d 道（选项固定为 [\"正确\", \"错误\"]）。" % (
            mix.get("single", 0), mix.get("multi", 0), mix.get("judge", 0)),
        "选项文字长度接近、干扰项合理；不要用「以上都对」「以上都不对」「无法确定」这类选项；"
        "不要让两道题的题干重复或只是换了数字。",
        "每题必须有 explain（一句话解析，说明为什么）、topic（具体知识点名称，"
        "如「两位数进位加法」「古诗文默写」「一般现在时第三人称单数」，"
        "不要写「综合」「其他」这种笼统词）、star（1~5 的难度星级）。",
        "答案字段 answer：单选/判断写选项序号（0 开始）或字母 A/B/C/D；"
        "判断题也可以写「正确」或「错误」；多选题写序号数组如 [0,2] 或字母 \"A,C\"。",
    ]
    if multi:
        reqs.append("本次要混合多个科目，每题的 subject 字段只能写实际所属的那一科"
                    "（%s），不要写「综合」「多科」，也不要把不同科目的知识混进同一道题。"
                    % "、".join(subjects))
    if topics:
        reqs.append("优先围绕这些知识点出题：%s；其中至少一半题目必须命中上述知识点"
                    "（薄弱知识点要反复练）。" % "、".join(topics[:8]))
    lines = [head, ""] + plines + ["", "【命题要求】"]
    lines += ["%d. %s" % (i, text) for i, text in enumerate(reqs, 1)]
    if variant_text:
        lines += [
            "",
            "【变式要求】下面是孩子做错过的题目。请针对相同知识点重新出题："
            "换情境、换数值或换提问角度，但不要照抄原题，也不要让答案能靠背口诀猜出来。",
            variant_text[:1800],
        ]
    lines += [
        "",
        "【输出格式】只输出 JSON 数组，每个元素字段如下：",
        '{"type":"single|multi|judge","text":"题干","options":["选项0","选项1",...],'
        '"answer":0,"explain":"解析","topic":"知识点","star":2,'
        '"stage":"%s","grade":"%s","subject":"%s"}' % (
            stage, grade, subject if not multi else "科目名"),
    ]
    if multi:
        lines.append("上面的 subject 只能是：%s。每道题写它实际所属的那一科。" %
                     "、".join(subjects))
    return "\n".join(lines)


def split_mix(count, mix=None):
    """题量按单选 50% / 多选 25% / 判断 25% 分配，保证三种题型都出现。"""
    count = max(1, min(30, int(count or 10)))
    if isinstance(mix, dict) and any(mix.get(k) for k in TYPES):
        want = {k: max(0, int(mix.get(k) or 0)) for k in TYPES}
        total = sum(want.values())
        if total > count:
            scale = count / total
            want = {k: int(v * scale) for k, v in want.items()}
        left = count - sum(want.values())
        for k in ("single", "multi", "judge"):
            if left <= 0:
                break
            want[k] += 1
            left -= 1
        return want
    if count < 3:
        return {"single": count, "multi": 0, "judge": 0}
    single = max(1, int(round(count * 0.5)))
    multi = max(1, int(round(count * 0.25)))
    judge = max(1, count - single - multi)
    return {"single": single, "multi": multi, "judge": judge}


# ---------------------------------------------------------------- 答案解析

def _parse_answer(value, qtype, n_options):
    # 3.9.0 修复：纯数字答案（尤其是 0）以前会因 "value or ''" 被当成空值整题丢掉
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        parts = [str(int(value))]
    elif isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value]
    else:
        parts = re.split(r"[,，、;；/|\s]+",
                         str(value if value is not None else "").strip())
    idxs = []
    for part in parts:
        if not part:
            continue
        low = part.lower()
        idx = None
        if qtype == "judge":
            if low in ("对", "正确", "√", "true", "t", "yes", "y", "a", "0"):
                idx = 0
            elif low in ("错", "错误", "×", "x", "false", "f", "no", "n", "b", "1"):
                idx = 1
        if idx is None:
            if re.fullmatch(r"[A-Ha-h]", part):
                idx = LETTERS.index(part.upper())
            elif re.fullmatch(r"\d+", part):
                idx = int(part)
            else:
                m = re.match(r"^([A-Ha-h])\b", part) or re.search(r"[A-Ha-h]", part)
                if m:
                    idx = LETTERS.index(m.group(1).upper())
        if idx is None or not 0 <= idx < n_options or idx in idxs:
            continue
        idxs.append(idx)
    if not idxs:
        raise AIError("AI 返回的答案无法解析：%r" % (value,))
    if qtype == "multi":
        if len(idxs) < 2:
            raise AIError("多选题至少要 2 个正确答案")
        return sorted(idxs)
    return idxs[0]


def _escape_control(text):
    """把 JSON 字符串里的裸控制字符（换行/制表符等）转义。

    模型偶尔把真换行直接写进 JSON 字符串，严格解析会报
    ``Invalid control character``；转义后再解析，正常内容一字不动。
    """
    out, in_str, esc = [], False, False
    for ch in str(text or ""):
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                in_str = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}
                           .get(ch, "\\u%04x" % ord(ch)))
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _extract_json(content):
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        # 兜底 1：转义裸控制字符（最常见的 Invalid control character）
        # 兜底 2：宽松模式，允许字符串里出现控制字符
        try:
            data = json.loads(_escape_control(text))
        except ValueError:
            try:
                data = json.loads(_escape_control(text), strict=False)
            except ValueError as exc:
                raise AIError("AI 没有返回合法 JSON：%s" % exc)
    if not isinstance(data, list):
        if isinstance(data, dict):
            for key in ("questions", "items", "data", "result"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise AIError("AI 返回内容不是题目数组")
    return data


def normalize_ai_questions(raw, spec, source="ai"):
    """AI 输出 → 统一题目结构，逐题校验，坏题跳过而不是整批失败。"""
    stage = str(spec.get("stage") or "")
    grade = str(spec.get("grade") or "")
    subjects = spec_subjects(spec)
    single = subjects[0] if len(subjects) == 1 else ""

    def _subject_of(value):
        """把 AI 写的科目收敛到本次勾选的科目里；对不上就留空（不瞎猜）。"""
        name = str(value or "").strip()[:16]
        if not name:
            return single
        for known in subjects:
            if name == known or known in name or name in known:
                return known
        return "" if len(subjects) > 1 else name

    out = []
    for i, item in enumerate(raw[:60], 1):
        if not isinstance(item, dict):
            continue
        qtype = store.norm_type(item.get("type"))
        opts = item.get("options") or []
        if not isinstance(opts, list):
            continue
        opts = [str(o).strip() for o in opts if str(o).strip()]
        if qtype == "judge":
            opts = ["正确", "错误"]
        if not 2 <= len(opts) <= 8 or len(set(opts)) != len(opts):
            continue
        try:
            answer = _parse_answer(item.get("answer"), qtype, len(opts))
        except AIError:
            continue
        topic = str(item.get("topic") or "").strip()[:24]
        q = {
            "type": qtype, "text": str(item.get("text") or "").strip()[:400],
            "options": opts, "answer": answer,
            "explain": str(item.get("explain") or "").strip()[:400],
            "topic": topic, "star": item.get("star", 2),
            "stage": str(item.get("stage") or stage),
            "grade": str(item.get("grade") or grade),
            "subject": _subject_of(item.get("subject")),
            "source": source,
        }
        if not q["text"]:
            continue
        if not q["topic"]:
            q["topic"] = learning.topic_of(q)
        try:
            clean = store.validate_question(q, i)
        except store.StoreError:
            continue
        clean["topic"] = q["topic"]
        clean["source"] = source
        out.append(clean)
    return out


# ---------------------------------------------------------------- 离线生成

GZ_PLACES = ["广州塔", "珠江新城", "越秀公园", "陈家祠", "沙面", "白云山",
             "海心沙", "北京路", "上下九", "荔枝湾涌", "南沙湿地", "天河体育中心"]
SCENES = ["广州塔研学", "陈家祠研学", "白云山研学", "珠江边研学", "地铁三号线",
          "早茶桌上", "越秀花市", "南沙湿地", "广东省博物馆", "荔枝湾涌"]
GZ_FOODS = ["虾饺", "烧卖", "叉烧包", "肠粉", "艇仔粥", "双皮奶"]
IDIOMS = [
    ("画蛇添足", "做了多余的事，反而把事情弄糟"),
    ("守株待兔", "死守经验，妄想不劳而获"),
    ("亡羊补牢", "出了问题及时补救"),
    ("掩耳盗铃", "自己欺骗自己"),
    ("井底之蛙", "见识狭小的人"),
    ("刻舟求剑", "拘泥成法，不知变通"),
    ("胸有成竹", "做事之前已有通盘打算"),
    ("南辕北辙", "行动和目的相反"),
]
POEMS = [
    ("举头望明月", "低头思故乡"), ("春眠不觉晓", "处处闻啼鸟"),
    ("锄禾日当午", "汗滴禾下土"), ("白日依山尽", "黄河入海流"),
    ("两个黄鹂鸣翠柳", "一行白鹭上青天"), ("欲穷千里目", "更上一层楼"),
]
WORDS_EN = [
    ("metro", "地铁"), ("dim sum", "点心"), ("library", "图书馆"),
    ("ticket", "车票"), ("bridge", "桥"), ("museum", "博物馆"),
    ("umbrella", "雨伞"), ("harbour", "港口"), ("flower", "花"), ("station", "车站"),
]
SPRING_WORDS = ["春暖花开", "鸟语花香", "草长莺飞", "烈日炎炎", "冰天雪地", "硕果累累"]
TRANSPORTS = ["bus", "metro", "apple", "ferry", "rice", "bicycle"]
ENERGY = ["太阳能", "风能", "煤炭", "石油", "水能"]


def _shuffled_options(correct, distractors, n=4):
    opts = [str(correct)] + [str(d) for d in distractors if str(d) != str(correct)]
    seen, out = set(), []
    for o in opts:
        if o not in seen:
            seen.add(o)
            out.append(o)
    out = out[:n]
    idx = out.index(str(correct)) if str(correct) in out else 0
    random.shuffle(out)
    return out, out.index(str(correct)) if str(correct) in out else idx


def _base(spec, qtype, topic, star=2, source="offline"):
    return {
        "type": qtype, "topic": topic, "star": star, "source": source,
        "stage": str(spec.get("stage") or ""),
        "grade": str(spec.get("grade") or ""),
        "subject": str(spec.get("subject") or ""),
    }


def _math(spec, qtype):
    place = random.choice(GZ_PLACES)
    food = random.choice(GZ_FOODS)
    a, b = random.randint(11, 89), random.randint(3, 39)
    if qtype == "judge":
        real = a * b
        shown = real if random.random() < 0.5 else real + random.choice([-b, b, 1, -1])
        q = _base(spec, "judge", "乘除法")
        q.update(text="算式 %d × %d = %d，这个结果对吗？" % (a, b, shown),
                 options=["正确", "错误"], answer=0 if shown == real else 1,
                 explain="%d × %d = %d。" % (a, b, real))
        return q
    if qtype == "multi":
        target = a + b
        pairs = [(a, b), (a + 1, b - 1), (a + 2, b - 2)]
        wrong = [(a + 5, b + 1), (a - 3, b + 9)]
        exprs = ["%d + %d" % p for p in pairs + wrong]
        random.shuffle(exprs)
        answer = sorted(exprs.index("%d + %d" % p) for p in pairs)
        q = _base(spec, "multi", "加减法")
        q.update(text="下面哪些算式的得数等于 %d？" % target,
                 options=exprs, answer=answer,
                 explain="%s 的得数都是 %d。" % ("、".join("%d + %d" % p for p in pairs), target))
        return q
    if random.random() < 0.5:
        total = a + b
        opts, idx = _shuffled_options(total, [total + 1, total - 1, total + b, a - b])
        q = _base(spec, "single", "加减法")
        q.update(text="周末去%s游玩，上午来了 %d 人，下午又来了 %d 人，"
                      "这天一共来了多少人？" % (place, a, b),
                 options=opts, answer=idx, explain="%d + %d = %d（人）。" % (a, b, total))
        return q
    total = a * b
    opts, idx = _shuffled_options(total, [total + a, total - b, total + 1, a + b])
    q = _base(spec, "single", "乘除法")
    q.update(text="广州早茶一笼%s有 %d 个，%d 笼一共有多少个？" % (food, b, a),
             options=opts, answer=idx, explain="%d × %d = %d（个）。" % (b, a, total))
    return q


def _chinese(spec, qtype):
    if qtype == "judge":
        word, meaning = random.choice(IDIOMS)
        flip = random.random() < 0.5
        wrong_meaning = random.choice([m for _, m in IDIOMS if m != meaning])
        shown = wrong_meaning if flip else meaning
        q = _base(spec, "judge", "成语与积累")
        q.update(text="成语「%s」的意思是「%s」，这个说法对吗？" % (word, shown),
                 options=["正确", "错误"], answer=1 if flip else 0,
                 explain="「%s」指：%s。" % (word, meaning))
        return q
    if qtype == "multi":
        banks = [
            (["春暖花开", "鸟语花香", "草长莺飞", "烈日炎炎", "冰天雪地", "硕果累累"],
             ["春暖花开", "鸟语花香", "草长莺飞"],
             "下面哪些词语是描写春天景色的？", "春暖花开、鸟语花香、草长莺飞都写春景。", "字词基础"),
            (["硕果累累", "秋高气爽", "金桂飘香", "春暖花开", "烈日炎炎", "鹅毛大雪"],
             ["硕果累累", "秋高气爽", "金桂飘香"],
             "下面哪些词语是描写秋天景色的？", "硕果累累、秋高气爽、金桂飘香都写秋景。", "字词基础"),
            (["画蛇添足", "亡羊补牢", "守株待兔", "井底之蛙", "春暖花开", "胸有成竹"],
             ["画蛇添足", "亡羊补牢", "守株待兔", "井底之蛙"],
             "下面哪些成语里含有动物的名称？", "蛇、羊、兔、蛙都是动物。", "成语与积累"),
        ]
        words, correct, text, explain, topic = random.choice(banks)
        opts = words[:]
        random.shuffle(opts)
        answer = sorted(opts.index(w) for w in correct)
        q = _base(spec, "multi", topic)
        q.update(text=text, options=opts, answer=answer, explain=explain)
        return q
    if random.random() < 0.5:
        word, meaning = random.choice(IDIOMS)
        others = random.sample([m for _, m in IDIOMS if m != meaning], 3)
        opts, idx = _shuffled_options(meaning, others)
        q = _base(spec, "single", "成语与积累")
        q.update(text="成语「%s」的意思是什么？" % word, options=opts,
                 answer=idx, explain="「%s」指：%s。" % (word, meaning))
        return q
    first, second = random.choice(POEMS)
    others = random.sample([s for f, s in POEMS if s != second], 3)
    opts, idx = _shuffled_options(second, others)
    q = _base(spec, "single", "古诗文")
    q.update(text="「%s」的下一句是？" % first, options=opts,
             answer=idx, explain="原句：%s，%s。" % (first, second))
    return q


def _english(spec, qtype):
    if qtype == "judge":
        en, zh = random.choice(WORDS_EN)
        flip = random.random() < 0.5
        wrong_zh = random.choice([z for _, z in WORDS_EN if z != zh])
        shown = wrong_zh if flip else zh
        q = _base(spec, "judge", "词汇释义")
        q.update(text='单词 "%s" 的中文意思是「%s」，这个说法对吗？' % (en, shown),
                 options=["正确", "错误"], answer=1 if flip else 0,
                 explain='"%s" 的意思是「%s」。' % (en, zh))
        return q
    if qtype == "multi":
        banks = [
            (TRANSPORTS, ["bus", "metro", "ferry", "bicycle"],
             "Which of the following are means of transport?（哪些是交通工具？）",
             "bus / metro / ferry / bicycle 都是交通工具。"),
            (["dim sum", "rice noodles", "shrimp dumpling", "ticket", "umbrella", "station"],
             ["dim sum", "rice noodles", "shrimp dumpling"],
             "Which of the following are Cantonese food?（哪些是广式点心？）",
             "dim sum / rice noodles / shrimp dumpling 都是广式点心。"),
            (["library", "museum", "station", "umbrella", "ticket", "bridge"],
             ["library", "museum", "station"],
             "Which of the following are places?（哪些是场所？）",
             "library / museum / station 都是场所。"),
        ]
        words, correct, text, explain = random.choice(banks)
        opts = words[:]
        random.shuffle(opts)
        answer = sorted(opts.index(w) for w in correct)
        q = _base(spec, "multi", "词汇释义")
        q.update(text=text, options=opts, answer=answer, explain=explain)
        return q
    if random.random() < 0.5:
        en, zh = random.choice(WORDS_EN)
        others = random.sample([z for _, z in WORDS_EN if z != zh], 3)
        opts, idx = _shuffled_options(zh, others)
        q = _base(spec, "single", "词汇释义")
        q.update(text='在广州地铁里看到 "%s"，它的中文意思是？' % en,
                 options=opts, answer=idx, explain='"%s" 意为「%s」。' % (en, zh))
        return q
    q = _base(spec, "single", "时态与语法")
    opts = ["go", "goes", "going", "went"]
    q.update(text="He ___ to school by Guangzhou Metro every day.",
             options=opts, answer=1,
             explain="every day 表示一般现在时，主语 He 是第三人称单数，动词加 -es。")
    return q


def _science(spec, qtype):
    topic = "乡土地理" if "地理" in spec.get("subject", "") else "科学常识"
    if qtype == "judge":
        banks = [
            ("广州地处亚热带，夏季高温多雨。", True,
             "广州位于亚热带季风气候区，夏季高温多雨、冬季温和少雨。"),
            ("标准大气压下水的沸点是 100 ℃。", True, "标准大气压下水的沸点为 100 ℃。"),
            ("地球自转一周大约需要 24 小时。", True, "地球自转一周约 24 小时，形成昼夜交替。"),
            ("广州冬季常年冰雪覆盖。", False, "广州冬季温和少雨，不会常年冰雪覆盖。"),
            ("植物进行光合作用不需要阳光。", False, "光合作用需要光能、水和二氧化碳。"),
        ]
        text, truth, explain = random.choice(banks)
        q = _base(spec, "judge", topic)
        q.update(text=text + "这个说法对吗？", options=["正确", "错误"],
                 answer=0 if truth else 1, explain=explain)
        return q
    if qtype == "multi":
        banks = [
            (ENERGY, ["太阳能", "风能", "水能"],
             "下列哪些属于可再生能源？", "太阳能、风能、水能可持续再生，煤炭和石油不可再生。", "能源与环境"),
            (["铜丝", "铁丝", "铝片", "橡胶", "塑料", "干木棒"], ["铜丝", "铁丝", "铝片"],
             "下列哪些材料容易导电？", "金属一般都容易导电，橡胶、塑料、干木棒是绝缘体。", "物质与能量"),
            (["冰融化", "水蒸发", "玻璃破碎", "铁生锈", "纸燃烧"], ["冰融化", "水蒸发", "玻璃破碎"],
             "下列哪些属于物理变化？", "物理变化不生成新物质；铁生锈、纸燃烧会生成新物质。", "物质与能量"),
        ]
        words, correct, text, explain, t = random.choice(banks)
        opts = words[:]
        random.shuffle(opts)
        answer = sorted(opts.index(w) for w in correct)
        q = _base(spec, "multi", t)
        q.update(text=text, options=opts, answer=answer, explain=explain)
        return q
    banks = [
        ("100 ℃", ["0 ℃", "50 ℃", "37 ℃"], "在标准大气压下，水的沸点是多少？",
         "标准大气压下水的沸点是 100 ℃。"),
        ("阳光", ["月光", "声音", "土壤"], "植物进行光合作用主要需要哪种能量？",
         "光合作用把光能转化为化学能，主要能量来源是阳光。"),
        ("亚热带季风气候", ["热带雨林气候", "寒带气候", "地中海气候"],
         "广州所属的主要气候类型是？", "广州位于亚热带季风气候区，夏季高温多雨。"),
    ]
    correct, distractors, text, explain = random.choice(banks)
    opts, idx = _shuffled_options(correct, distractors)
    q = _base(spec, "single", topic)
    q.update(text=text, options=opts, answer=idx, explain=explain)
    return q


def offline_generate(spec, avoid=None):
    """不联网也能出的随机题：广州情境 + 三种题型 + 知识点标签。

    3.9.0 起支持科目多选：题量按科目平均分配，每科用自己的模板出题，
    再把结果打散，保证每个勾选的科目都有题、且都是该科的内容。
    """
    count = max(1, min(30, int(spec.get("count") or 10)))
    subjects = spec_subjects(spec)
    alloc = allocate(count, len(subjects))
    avoid = {str(t).strip() for t in (avoid or [])}
    out, seen = [], set(avoid)
    for subject, quota in zip(subjects, alloc):
        if quota <= 0:
            continue
        mix = split_mix(quota, spec.get("mix"))
        # split_mix 为极小配额会补齐三种题型，可能超出本科配额，这里收敛到刚好 quota 道
        while sum(mix.values()) > quota:
            for k in ("multi", "judge", "single"):
                if sum(mix.values()) <= quota:
                    break
                if mix.get(k):
                    mix[k] -= 1
        while sum(mix.values()) < quota:
            mix["single"] = mix.get("single", 0) + 1
        builder = {"数学": _math, "语文": _chinese,
                   "英语": _english}.get(subject, _science)
        sub_spec = dict(spec, subject=subject, subjects=[subject], count=quota)
        for qtype in ("single", "multi", "judge"):
            for _ in range(mix.get(qtype, 0)):
                for attempt in range(40):
                    q = builder(sub_spec, qtype)
                    if subject != "数学":
                        # 广州研学练习册编号：同一知识点也能出多道不重复的题
                        q["text"] = "【%s·练习%02d】%s" % (
                            random.choice(SCENES), random.randint(1, 99), q["text"])
                    if q["text"] in seen:
                        continue
                    try:
                        clean = store.validate_question(q, len(out) + 1)
                    except store.StoreError:
                        continue
                    clean["topic"] = q["topic"]
                    clean["subject"] = subject
                    clean["source"] = "offline"
                    seen.add(q["text"])
                    out.append(clean)
                    break
    random.shuffle(out)
    return out


# ---------------------------------------------------------------- 对外入口

def dedupe(questions, avoid=None):
    avoid = {str(t).strip() for t in (avoid or [])}
    out = []
    for q in questions:
        if q["text"] in avoid:
            continue
        avoid.add(q["text"])
        out.append(q)
    return out


def generate(spec, force_offline=False):
    """生成题目 → {questions, source, note, prompt?, latency?}。"""
    spec = dict(spec or {})
    spec.setdefault("stage", "小学")
    # 3.9.0：科目可多选；内部统一用 subjects 列表，subject 保留兼容与展示
    subjects = spec_subjects(spec)
    spec["subjects"] = subjects
    spec["subject"] = subjects[0] if len(subjects) == 1 else "、".join(subjects)
    spec.setdefault("grade", "")
    count = max(1, min(30, int(spec.get("count") or 10)))
    spec["count"] = count
    mix = split_mix(count, spec.get("mix"))
    cfg = load_config()
    # 教材版本与「手动教学进度」来自后台配置，注入 spec 供 prompt 使用
    spec["textbook"] = cfg["textbook"]
    if not cfg.get("progress_auto", True) and str(cfg.get("term_scope") or "").strip():
        spec["scope"] = cfg["term_scope"]
    prog = progress.current_progress(scope=spec.get("scope"))
    mode = str(spec.get("mode") or "auto")
    offline_only = force_offline or mode == "offline" or not cfg.get("api_key")
    if offline_only:
        questions = offline_generate(dict(spec, mix=mix), spec.get("avoid"))
        return {
            "questions": questions, "source": "offline", "mix": mix,
            "progress": prog,
            "note": ("未配置 API Key，已用离线模板生成（题目带广州情境，题型混合）。"
                     "在上方填写 Key 后可生成更自然、覆盖更广的新题。"),
        }

    variant_text = ""
    if spec.get("wrong_items"):
        variant_text = "\n".join(
            "- [%s / %s] %s（答案：%s）" %
            (w.get("subject", ""), w.get("topic", ""), str(w.get("text", ""))[:110],
             str(w.get("answer", ""))[:40])
            for w in spec["wrong_items"][:8])
    prompt = build_prompt(spec, mix, variant_text)
    try:
        content, latency = _chat(cfg, SYSTEM_PROMPT, prompt)
        raw = _extract_json(content)
        questions = normalize_ai_questions(raw, spec, "ai")
    except AIError as exc:
        if mode == "ai" or not cfg.get("auto_offline"):
            raise
        questions = offline_generate(dict(spec, mix=mix), spec.get("avoid"))
        return {
            "questions": questions, "source": "offline", "mix": mix,
            "progress": prog,
            "note": "AI 接口调用失败（%s），已自动改用离线模板生成。" % exc,
        }
    questions = dedupe(questions, spec.get("avoid"))
    if len(questions) < count and cfg.get("auto_offline"):
        extra = offline_generate(dict(spec, count=count - len(questions), mix=mix),
                                 spec.get("avoid"))
        questions = dedupe(questions + extra, spec.get("avoid"))
    return {
        "questions": questions[:count], "source": "ai", "mix": mix,
        "model": cfg["model"], "latency": latency, "progress": prog,
        "note": "AI 生成 %d 道（%s），已通过题目格式校验。" % (len(questions[:count]), cfg["model"]),
    }


def generate_for_device(spec, ability_row, cfg=None):
    """针对某台设备：弱项知识点 + 到期错题变式题。"""
    spec = dict(spec or {})
    weak = [w["topic"] for w in (ability_row.get("weak") or [])][:5]
    wrong = ability_row.get("wrong") or []
    due = [w for w in wrong if w.get("due")] or wrong
    spec["topics"] = weak or spec.get("topics")
    spec["wrong_items"] = [{
        "subject": w.get("subject", ""), "topic": w.get("topic", ""),
        "text": w.get("text", ""), "answer": "",
    } for w in due[:8]]
    spec.setdefault("mode", "auto")
    return generate(spec)
