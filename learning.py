#!/usr/bin/env python3
"""学情模型：错题本、知识点掌握度、弱项识别与智能组卷。

3.18.0 起学情按「人」记录：一个人 = 一套题库（bank_id），不再按设备 MAC 区分。
同一套题库下不管孩子用 iPad、手机、电视还是家长代答，学情都算同一个人。
数据落在 state/learning.json（不入库、不随代码更新覆盖）：
  learners[bank_id] = {
    history: 最近若干次逐题作答记录,
    wrong:   错题本（含题目快照、知识点、复习到期时间）,
    topics:  每个知识点的对错次数与 EWMA 掌握度,
    rounds:  答题轮次统计,
    diff:    目标难度（按最近正确率自动调整）,
  }
旧版文件（devices[mac]）会在服务启动时按「设备→题库」自动合并迁移。

组卷不再是纯随机（3.13.0 起按四层优先级取题）：
  1) 错题（答错过、还没答对过的）优先，占约 40%——复习到期的先出
  2) 没做过的新题占约 40%，保证进度推进
  3) 以前错过、后来连对「毕业」的题占约 10%，少量复习防遗忘
  4) 做过且一直答对的已会题占约 10%，热身维持
  某层题不够时份额自动顺延给后面的层；层内先按弱项知识点加权、
  其次优先离目标难度（按最近正确率算出）近的题。
  另保留：弱项加权（层内排序）、同一知识点一轮最多占一半、
  尽量三种题型都出现、一轮之内不重复、乱序出题。

3.15.0 起按正确率自适应难度：每答 5 题看最近 20 题的正确率，
低于 90% 就把出题难度（1~5 星）降 0.5、高于 96% 升 0.25，
让正确率尽量稳定在 90% 以上。AI 出题/自动出题会把星级写进 prompt。
"""

import json
import math
import os
import random
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
PATH = os.path.join(STATE, "learning.json")

HISTORY_LIMIT = 400
WRONG_LIMIT = 200
TOPIC_LIMIT = 120
_lock = threading.Lock()

# 答错后的复习间隔（秒）：越快重逢越能记牢，连续答对则逐步拉长
REVIEW_AFTER = [2 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 15 * 86400]
WEAK_THRESHOLD = 0.62       # EWMA 掌握度低于此值算弱项
WEAK_MIN_SAMPLES = 2        # 至少答过 2 次才下判断
EMA_ALPHA = 0.35

# ---- 自适应难度（3.15.0）----
# 目标：让孩子的正确率稳定在 90% 以上（90%~96% 之间不调整，避免来回抖）。
# 难度用 1~5 星表示（和题目 star 字段一致），每答 5 题按最近 ACC_WINDOW 题
# 的正确率调一次：低于 90% 降难度、高于 96% 升难度。
DIFF_MIN, DIFF_MAX = 1.0, 5.0
DIFF_DEFAULT = 2.0
ACC_WINDOW = 20            # 近期正确率窗口（最近多少题）
DIFF_EVERY = 5             # 每答多少题调整一次
DIFF_STEP_DOWN = 0.5       # 正确率不足时降多少星
DIFF_STEP_UP = 0.25        # 正确率很高时升多少星
DIFF_LOW, DIFF_HIGH = 0.90, 0.96

# ---------------------------------------------------------------- 知识点

# 老题库没有 topic 字段，按「科目 + 题干关键词」即时归类，零迁移成本。
TOPIC_RULES = {
    "数学": [
        (("分数", "约分", "通分", "分子", "分母"), "分数"),
        (("小数", "小数点"), "小数"),
        (("百分", "比例", "折扣", "利率"), "百分数与比例"),
        (("面积", "周长", "体积", "表面积", "三角形", "正方形", "长方形", "圆", "角"), "图形与几何"),
        (("方程", "解方程", "未知数", "x =", "x="), "方程与代数"),
        (("倍数", "因数", "质数", "合数", "整除", "余数"), "数与整除"),
        (("平均", "统计", "概率", "可能性", "条形", "折线"), "统计与概率"),
        (("单位", "换算", "千克", "公里", "厘米", "毫升"), "量与单位"),
        (("时钟", "时间", "分钟", "小时"), "时间与钟表"),
        (("乘", "×", "*"), "乘除法"),
        (("加", "减", "+", "-", "一共", "剩下"), "加减法"),
    ],
    "语文": [
        (("古诗", "诗句", "默写", "《", "作者", "李白", "杜甫"), "古诗文"),
        (("成语", "寓言", "歇后语"), "成语与积累"),
        (("近义", "反义", "多音", "拼音", "读音", "错别字", "字义", "词义"), "字词基础"),
        (("比喻", "拟人", "修辞", "排比", "夸张"), "修辞手法"),
        (("病句", "标点", "关联词", "句子"), "句子与标点"),
        (("广州", "岭南", "粤", "早茶", "醒狮"), "岭南文化常识"),
        (("阅读", "片段", "短文"), "阅读理解"),
    ],
    "英语": [
        (("一般现在", "第三人称", "过去式", "现在进行", "时态", "yesterday", "tomorrow"), "时态与语法"),
        (("复数", "plural", "名词"), "名词与复数"),
        (("介词", "冠词", "a ", "an ", "the "), "介词与冠词"),
        (("意思", "翻译", "中文", "means", "mean"), "词汇释义"),
        (("对话", "情景", "问候", "thank", "hello"), "情景交际"),
    ],
    "物理": [(("力", "重力", "摩擦"), "力与运动"), (("电", "电流", "电压", "电阻"), "电学"),
             (("光", "透镜", "反射"), "光学"), (("热", "温度", "比热"), "热学")],
    "化学": [(("化学方程", "反应", "燃烧"), "化学反应"), (("溶液", "酸碱", "ph", "PH"), "溶液与酸碱"),
             (("元素", "原子", "分子"), "物质构成")],
    "生物": [(("细胞", "组织", "器官"), "生物体结构"), (("光合", "呼吸", "植物"), "植物生理"),
             (("遗传", "基因", "染色体"), "遗传与变异")],
    "历史": [(("朝代", "秦", "汉", "唐", "宋", "明", "清"), "中国古代史"),
             (("近代", "鸦片战争", "辛亥", "五四"), "中国近代史")],
    "地理": [(("广州", "广东", "珠江", "岭南"), "乡土地理"), (("气候", "季风", "降水"), "气候"),
             (("地图", "经纬", "等高线"), "地图与地形")],
    "科学": [(("光", "声", "电", "磁"), "物质与能量"), (("植物", "动物", "生态"), "生命科学"),
             (("太阳", "地球", "月亮", "星"), "地球与宇宙")],
}


def topic_of(q):
    """返回题目知识点：优先用自带 topic，否则按题干推断。"""
    explicit = str((q or {}).get("topic") or "").strip()
    if explicit:
        return explicit[:24]
    subject = str((q or {}).get("subject") or "").strip()
    text = str((q or {}).get("text") or "").strip()
    low = text.lower()
    rules = TOPIC_RULES.get(subject, [])
    for words, topic in rules:
        if any(w.lower() in low for w in words):
            return topic
    generic = {
        "数学": "数与运算", "语文": "语文综合", "英语": "英语综合",
        "物理": "物理综合", "化学": "化学综合", "生物": "生物综合",
        "历史": "历史综合", "地理": "地理综合", "科学": "科学综合",
    }
    return generic.get(subject, subject or "综合知识")


# ---------------------------------------------------------------- 读写

def _empty_device():
    return {"history": [], "wrong": [], "topics": {},
            "rounds": {"total": 0, "perfect": 0, "last": 0}}


def _read_raw():
    if not os.path.exists(PATH):
        return {}
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load():
    """读出学情：统一成 {"version": 2, "learners": {...}}。

    旧文件是 {"devices": {mac: {...}}}，这里先按 MAC 原样读进来（key 仍是 mac），
    真正的「MAC→题库」合并由 migrate() 在服务启动时做一次。
    """
    raw = _read_raw()
    rows = raw.get("learners")
    if not isinstance(rows, dict):
        rows = raw.get("devices")            # 旧格式
    if not isinstance(rows, dict):
        rows = {}
    return {"version": 2, "learners": rows}


def _save(data):
    os.makedirs(STATE, exist_ok=True)
    out = {"version": 2, "learners": data.get("learners") or {}}
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)


def _dev(data, who, create=True):
    """取/建某个学习者（= 题库）的学情记录。"""
    who = str(who or "").strip().lower()
    rows = data.setdefault("learners", {})
    dev = rows.get(who)
    if not dev and create:
        dev = _empty_device()
        rows[who] = dev
    if dev:
        dev.setdefault("history", [])
        dev.setdefault("wrong", [])
        dev.setdefault("topics", {})
        dev.setdefault("rounds", {"total": 0, "perfect": 0, "last": 0})
    return dev or _empty_device()


# ---------------------------------------------------------------- 记录

def record_answer(who, q, ok, ts=None):
    """每答一题记一次：知识点掌握度、历史、错题本与复习排期。

    who = 学习者（3.18.0 起 = 题库 id，一个人一套题库，不按设备区分）。
    """
    who = str(who or "").strip().lower()
    if not who:
        return
    ts = float(ts or time.time())
    topic = topic_of(q)
    with _lock:
        data = _load()
        dev = _dev(data, who)
        # 知识点：计数 + EWMA 掌握度（近期表现权重更高）
        rec = dev["topics"].get(topic) or {"right": 0, "wrong": 0, "ema": 0.5, "last": 0}
        rec["right" if ok else "wrong"] = int(rec.get("right" if ok else "wrong", 0)) + 1
        rec["ema"] = round(rec.get("ema", 0.5) * (1 - EMA_ALPHA) + (EMA_ALPHA if ok else 0), 4)
        rec["last"] = ts
        dev["topics"][topic] = rec
        if len(dev["topics"]) > TOPIC_LIMIT:
            keep = sorted(dev["topics"].items(), key=lambda kv: kv[1].get("last", 0),
                          reverse=True)[:TOPIC_LIMIT]
            dev["topics"] = dict(keep)

        dev["history"].append({
            "qid": q.get("id", ""), "topic": topic,
            "subject": q.get("subject", ""), "ok": bool(ok), "ts": int(ts),
        })
        dev["history"] = dev["history"][-HISTORY_LIMIT:]
        dev["diff"] = adjust_difficulty(dev)

        wrong = dev["wrong"]
        item = next((w for w in wrong if w.get("qid") == q.get("id", "")), None)
        if not ok:
            if item is None:
                item = {
                    "qid": q.get("id", ""), "text": q.get("text", ""),
                    "topic": topic, "subject": q.get("subject", ""),
                    "type": q.get("type", "single"),
                    "options": list(q.get("options") or []),
                    "answer": q.get("answer"), "explain": q.get("explain", ""),
                    "count": 0, "review_ok": 0, "last_wrong": 0,
                    "due_at": 0, "added": int(ts),
                }
                wrong.append(item)
            item["count"] = int(item.get("count", 0)) + 1
            item["review_ok"] = 0
            item["last_wrong"] = int(ts)
            step = REVIEW_AFTER[min(item["count"] - 1, len(REVIEW_AFTER) - 1)]
            item["due_at"] = int(ts + step)
            wrong.sort(key=lambda w: w.get("last_wrong", 0), reverse=True)
            dev["wrong"] = wrong[:WRONG_LIMIT]
        elif item is not None:
            # 复习题答对：连对两次「毕业」，否则拉长间隔再来一次
            item["review_ok"] = int(item.get("review_ok", 0)) + 1
            if item["review_ok"] >= 2:
                dev["wrong"] = [w for w in wrong if w is not item]
            else:
                item["due_at"] = int(ts + REVIEW_AFTER[min(item["count"], len(REVIEW_AFTER) - 1)])
        _save(data)


def _merge_dev(a, b):
    """把两份学情合并成一份（迁移：同一套题库下多台设备 → 一个人）。"""
    a, b = (a or {}), (b or {})
    hist = list(a.get("history") or []) + list(b.get("history") or [])
    hist.sort(key=lambda h: int(h.get("ts") or 0))
    hist = hist[-HISTORY_LIMIT:]

    wrong = {}
    for src in (a.get("wrong") or [], b.get("wrong") or []):
        for item in src:
            qid = str(item.get("qid") or "")
            cur = wrong.get(qid)
            if not cur:
                wrong[qid] = dict(item)
                continue
            cur["count"] = int(cur.get("count", 0)) + int(item.get("count", 0))
            if int(item.get("last_wrong", 0)) >= int(cur.get("last_wrong", 0)):
                for k in ("last_wrong", "due_at", "review_ok", "text", "options",
                          "answer", "explain", "topic", "subject", "type"):
                    if k in item:
                        cur[k] = item[k]
    wrong_list = sorted(wrong.values(), key=lambda w: w.get("last_wrong", 0),
                        reverse=True)[:WRONG_LIMIT]

    topics = {}
    for src in (a.get("topics") or {}, b.get("topics") or {}):
        for t, r in src.items():
            cur = topics.get(t)
            if not cur:
                topics[t] = dict(r)
                continue
            n1 = int(cur.get("right", 0)) + int(cur.get("wrong", 0))
            n2 = int(r.get("right", 0)) + int(r.get("wrong", 0))
            cur["right"] = int(cur.get("right", 0)) + int(r.get("right", 0))
            cur["wrong"] = int(cur.get("wrong", 0)) + int(r.get("wrong", 0))
            if n1 + n2:
                cur["ema"] = round((float(cur.get("ema", 0.5)) * n1 +
                                    float(r.get("ema", 0.5)) * n2) / (n1 + n2), 4)
            cur["last"] = max(int(cur.get("last", 0)), int(r.get("last", 0)))

    rounds = {"total": 0, "perfect": 0, "last": 0}
    for src in (a.get("rounds") or {}, b.get("rounds") or {}):
        rounds["total"] += int(src.get("total", 0))
        rounds["perfect"] += int(src.get("perfect", 0))
        rounds["last"] = max(rounds["last"], int(src.get("last", 0)))
    return {"history": hist, "wrong": wrong_list, "topics": topics,
            "rounds": rounds}


def migrate_bank_keys(mapping=None):
    """旧版「按设备 MAC」的学情 → 3.18.0「按题库（人）」的学情。

    mapping: {mac: bank_id}，由调用方从题库的设备归属里取。
    不在 mapping 里的 MAC 保持原样（数据不丢）。返回是否有改动。
    """
    mapping = {str(k).strip().lower(): str(v).strip().lower()
               for k, v in (mapping or {}).items()}
    raw = _read_raw()
    if raw.get("version") == 2 and isinstance(raw.get("learners"), dict):
        return False
    old = raw.get("devices") or raw.get("learners") or {}
    merged = {}
    for key, dev in old.items():
        target = mapping.get(str(key).strip().lower()) or str(key)
        merged[target] = _merge_dev(merged.get(target), dev)
    with _lock:
        _save({"learners": merged})
    return True


def recent_accuracy(dev, n=ACC_WINDOW):
    """最近 n 题的正确率；没答过返回 None。"""
    hist = (dev or {}).get("history") or []
    seg = hist[-max(1, int(n)):]
    if not seg:
        return None
    return sum(1 for h in seg if h.get("ok")) / float(len(seg))


def _initial_difficulty(dev):
    """老数据还没写 diff 时，先按最近正确率给一个起点（升级后第一批题就合适）。

    正确率越低起点越低；没有答题记录时用默认 2 星。
    """
    acc = recent_accuracy(dev)
    if acc is None:
        return DIFF_DEFAULT
    if acc >= DIFF_HIGH:
        return 3.0
    if acc >= DIFF_LOW:
        return 2.5
    if acc >= 0.80:
        return 2.0
    if acc >= 0.65:
        return 1.5
    return 1.0


def difficulty_of(dev):
    """返回 (难度值 1~5, 目标星级 1~5)。老数据没有 diff 字段时按正确率推算。"""
    try:
        raw = (dev or {}).get("diff")
        cur = None if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        cur = None
    if cur is None:
        cur = _initial_difficulty(dev)
    cur = max(DIFF_MIN, min(DIFF_MAX, cur))
    # 用「四舍五入」而不是银行家进位：1.5→2、2.5→3、3.5→4
    star = int(math.floor(cur + 0.5))
    return round(cur, 2), max(1, min(5, star))


def adjust_difficulty(dev):
    """按最近正确率调整目标难度，返回新的难度值。

    - 最近 20 题正确率 < 90%  → 降 0.5 星（题出简单点，先把正确率拉回 90%+）
    - 最近 20 题正确率 > 96%  → 升 0.25 星（太轻松了，加一点挑战）
    - 90%~96% 之间不动（这就是「正确率保持在 90% 以上」的目标区间）
    - 每答 5 题才调一次，且限幅 1~5 星，避免一题答错就剧烈变化
    """
    cur, _ = difficulty_of(dev)
    hist = (dev or {}).get("history") or []
    acc = recent_accuracy(dev)
    if acc is None or len(hist) < DIFF_EVERY:
        return cur
    if len(hist) % DIFF_EVERY:
        return cur
    if acc < DIFF_LOW:
        cur -= DIFF_STEP_DOWN
    elif acc > DIFF_HIGH:
        cur += DIFF_STEP_UP
    return round(max(DIFF_MIN, min(DIFF_MAX, cur)), 2)


def target_star(who):
    """供出题用：这个学习者（题库）当前应该出几星题（1~5）。"""
    with _lock:
        dev = _dev(_load(), who, create=False)
    return difficulty_of(dev)[1]


def bank_target_star(bank_id):
    """一套题库（= 一个人）该出几星题；没有答题记录返回 0（用默认难度）。"""
    a = ability(bank_id)
    if not a.get("total"):
        return 0
    return int(a.get("target_star") or 0) or 0


def record_round(who, all_ok, ts=None):
    who = str(who or "").strip().lower()
    if not who:
        return
    with _lock:
        data = _load()
        dev = _dev(data, who)
        r = dev["rounds"]
        r["total"] = int(r.get("total", 0)) + 1
        r["perfect"] = int(r.get("perfect", 0)) + (1 if all_ok else 0)
        r["last"] = int(ts or time.time())
        _save(data)


def delete_wrong_by_subject(who, subject):
    """3.39.0：删掉某个人错题本里某个学科的全部错题记录（不动题库、不动答对记录）。

    subject="" 或「未标注」→ 删除没有科目字段的错题。
    返回实际删掉的道数。
    """
    key = str(who or "").strip().lower()
    subject = str(subject or "").strip()
    if subject == "未标注":
        subject = ""
    removed = 0
    with _lock:
        data = _load()
        dev = (data.get("learners") or {}).get(key)
        if not dev:
            return 0
        keep = []
        for w in dev.get("wrong") or []:
            if (str(w.get("subject") or "").strip() == subject):
                removed += 1
            else:
                keep.append(w)
        if removed:
            dev["wrong"] = keep
            _save(data)
    return removed


def clear(who):
    """清空某个学习者（题库/人）的学情（不影响题库和放行记录）。"""
    key = str(who or "").strip().lower()
    with _lock:
        data = _load()
        if key in (data.get("learners") or {}):
            data["learners"].pop(key, None)
            _save(data)
            return True
    return False


def all_learners():
    """所有有学情记录的学习者（正常情况下 = 题库 id）。"""
    with _lock:
        return sorted(_load()["learners"].keys())


def all_devices():
    """兼容旧名（现在 key 是题库/人，不是设备 MAC）。"""
    return all_learners()


def done_qids(learner=None):
    """「做过」的题目 id 集合（history + 错题本）。

    传 learner（题库 id）时只算这个人做过的——3.18.0 起自动出题按题库统计
    「新题」，别把别的题库孩子做过的题算成做过。
    不传则返回全部学习者的并集。
    """
    with _lock:
        data = _load()
    rows = data.get("learners") or {}
    if learner is not None:
        key = str(learner or "").strip().lower()
        rows = {key: rows.get(key)} if key in rows else {}
    done = set()
    for dev in rows.values():
        for h in dev.get("history") or []:
            qid = str(h.get("qid") or "").strip()
            if qid:
                done.add(qid)
        for w in dev.get("wrong") or []:
            qid = str(w.get("qid") or "").strip()
            if qid:
                done.add(qid)
    return done


# ---------------------------------------------------------------- 分析

def weak_topics(dev, limit=6):
    out = []
    for topic, r in (dev.get("topics") or {}).items():
        n = int(r.get("right", 0)) + int(r.get("wrong", 0))
        ema = float(r.get("ema", 0.5))
        if n < WEAK_MIN_SAMPLES or ema >= WEAK_THRESHOLD:
            continue
        out.append({
            "topic": topic, "samples": n,
            "right": int(r.get("right", 0)), "wrong": int(r.get("wrong", 0)),
            "accuracy": round(int(r.get("right", 0)) / n, 3),
            "mastery": ema,
            "score": round((WEAK_THRESHOLD - ema) * math.sqrt(n) * 10, 3),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def weak_topics_for(who, limit=3):
    """供组卷使用：返回该学习者（题库）当前最薄弱的知识点名称列表。"""
    with _lock:
        dev = _dev(_load(), who, create=False)
    return [w["topic"] for w in weak_topics(dev, limit)]


def question_stats(who):
    """3.47.0：每道题的作答次数 / 答对 / 答错（按人=题库统计，题目页显示用）。"""
    key = str(who or "").strip().lower()
    if not key:
        return {}
    with _lock:
        dev = dict(_dev(_load(), key, create=False))
    out = {}
    for h in dev.get("history") or []:
        qid = str(h.get("qid") or "").strip()
        if not qid:
            continue
        rec = out.setdefault(qid, {"qid": qid, "total": 0, "right": 0,
                                   "wrong": 0, "last": 0})
        rec["total"] += 1
        if h.get("ok"):
            rec["right"] += 1
        else:
            rec["wrong"] += 1
        ts = int(h.get("ts") or 0)
        if ts > rec["last"]:
            rec["last"] = ts
    return out


def _bucket(ts):
    lt = time.localtime(ts)
    return time.strftime("%Y-%m-%d", lt)


def stats(who, now=None):
    """3.47.0：按天 / 周 / 月的时间序列 + 每科汇总（学习情况页图表用）。"""
    now = float(now or time.time())
    key = str(who or "").strip().lower()
    with _lock:
        dev = dict(_dev(_load(), key, create=False))
    hist = [h for h in (dev.get("history") or []) if h.get("ts")]

    def empty(label):
        return {"label": label, "total": 0, "right": 0, "wrong": 0, "accuracy": None}

    def add(row, h):
        row["total"] += 1
        if h.get("ok"):
            row["right"] += 1
        else:
            row["wrong"] += 1

    def finish(rows):
        for r in rows:
            r["accuracy"] = round(r["right"] / r["total"], 3) if r["total"] else None
        return rows

    # 每天：最近 30 天
    days = []
    day_index = {}
    for i in range(29, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        row = empty(d[5:])         # MM-DD
        row["date"] = d
        days.append(row)
        day_index[d] = row
    # 每周：最近 12 个自然周（周一开始）
    weeks = []
    week_index = {}
    lt = time.localtime(now)
    monday = now - ((lt.tm_wday) * 86400)
    for i in range(11, -1, -1):
        start = monday - i * 7 * 86400
        label = time.strftime("%m-%d", time.localtime(start))
        row = empty(label + " 周")
        row["date"] = time.strftime("%Y-%m-%d", time.localtime(start))
        weeks.append(row)
        week_index[row["date"]] = row
    # 每月：最近 12 个月
    months = []
    month_index = {}
    y, m = lt.tm_year, lt.tm_mon
    for i in range(11, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        label = "%d-%02d" % (yy, mm)
        row = empty(label)
        row["date"] = label
        months.append(row)
        month_index[label] = row

    subjects = {}
    for h in hist:
        ts = float(h["ts"])
        d = _bucket(ts)
        if d in day_index:
            add(day_index[d], h)
        lt2 = time.localtime(ts)
        monday2 = time.strftime("%Y-%m-%d",
                                time.localtime(ts - lt2.tm_wday * 86400))
        if monday2 in week_index:
            add(week_index[monday2], h)
        mon = time.strftime("%Y-%m", lt2)
        if mon in month_index:
            add(month_index[mon], h)
        subject = str(h.get("subject") or "").strip() or "未标注"
        row = subjects.setdefault(subject, {"subject": subject, "total": 0,
                                            "right": 0, "wrong": 0})
        add(row, h)
    for row in subjects.values():
        row["accuracy"] = round(row["right"] / row["total"], 3) if row["total"] else None
    subj_list = sorted(subjects.values(),
                       key=lambda x: (x["accuracy"] if x["accuracy"] is not None else 2,
                                      -x["total"]))
    return {"day": finish(days), "week": finish(weeks), "month": finish(months),
            "subjects": subj_list, "total": len(hist)}


def patch_wrong(qid, q, fields=("text", "options", "answer", "explain", "topic",
                                "subject", "type")):
    """3.34.0：后台改了题目后，把所有孩子错题本里这道题的副本一起更新。"""
    qid = str(qid or "").strip()
    if not qid:
        return 0
    patch = {k: q[k] for k in fields if k in q}
    if not patch:
        return 0
    changed = 0
    with _lock:
        data = _load()
        for dev in data.get("learners", {}).values():
            for w in dev.get("wrong") or []:
                if w.get("qid") == qid:
                    w.update(patch)
                    changed += 1
        if changed:
            _save(data)
    return changed


def subject_stats(who, now=None):
    """3.34.0：按学科统计正确率 / 错题，找最弱的一门。"""
    now = float(now or time.time())
    with _lock:
        dev = dict(_dev(_load(), who, create=False))
    stats = {}
    for h in dev.get("history") or []:
        subject = str(h.get("subject") or "").strip() or "未标注"
        row = stats.setdefault(subject, {"subject": subject, "total": 0,
                                         "correct": 0, "wrong_count": 0,
                                         "due_count": 0, "topics": {}})
        row["total"] += 1
        if h.get("ok"):
            row["correct"] += 1
    for w in dev.get("wrong") or []:
        subject = str(w.get("subject") or "").strip() or "未标注"
        row = stats.setdefault(subject, {"subject": subject, "total": 0,
                                         "correct": 0, "wrong_count": 0,
                                         "due_count": 0, "topics": {}})
        row["wrong_count"] += 1
        if w.get("due_at", 0) <= now:
            row["due_count"] += 1
        topic = str(w.get("topic") or "").strip()
        if topic:
            row["topics"][topic] = row["topics"].get(topic, 0) + 1
    out = []
    for row in stats.values():
        # 3.41.0：只显示「当前还有错题」的学科——某科错题删光后就不再出现在学科分析里
        if row["wrong_count"] <= 0:
            continue
        answered = row["total"]
        row["accuracy"] = round(row["correct"] / answered, 3) if answered else None
        row["weak_topics"] = [t for t, _n in sorted(
            row["topics"].items(), key=lambda kv: -kv[1])[:3]]
        del row["topics"]
        out.append(row)
    out.sort(key=lambda x: (x["accuracy"] if x["accuracy"] is not None else 2.0,
                            -x["wrong_count"]))
    weakest = None
    graded = [x for x in out if x["total"] >= 5]
    if graded:
        weakest = graded[0]["subject"]
    return {"subjects": out, "weakest": weakest}


def ability(who, now=None):
    """后台「学情」页用的汇总（who = 题库 id / 人）。"""
    now = float(now or time.time())
    with _lock:
        dev = dict(_dev(_load(), who, create=False))
    hist = dev.get("history") or []
    correct = sum(1 for h in hist if h.get("ok"))
    wrong = dev.get("wrong") or []
    rounds = dev.get("rounds") or {}
    topics = []
    for topic, r in (dev.get("topics") or {}).items():
        n = int(r.get("right", 0)) + int(r.get("wrong", 0))
        topics.append({
            "topic": topic, "samples": n,
            "right": int(r.get("right", 0)),
            "mastery": round(float(r.get("ema", 0.5)), 3),
        })
    topics.sort(key=lambda x: (x["mastery"], -x["samples"]))
    diff, star = difficulty_of(dev)
    _ss = subject_stats(who, now)
    return {
        "learner": str(who or "").strip().lower(),
        "total": len(hist), "correct": correct,
        "accuracy": round(correct / len(hist), 3) if hist else None,
        "difficulty": diff,
        "target_star": star,
        "recent_accuracy": (None if recent_accuracy(dev) is None
                            else round(recent_accuracy(dev), 3)),
        "rounds": int(rounds.get("total", 0)),
        "perfect": int(rounds.get("perfect", 0)),
        "last": int(rounds.get("last", 0)),
        "wrong_count": len(wrong),
        "due_count": sum(1 for w in wrong if w.get("due_at", 0) <= now),
        "weak": weak_topics(dev),
        "subjects": _ss["subjects"],
        "weakest_subject": _ss["weakest"],
        "topics": topics,
        "wrong": [{
            "qid": w.get("qid", ""), "text": w.get("text", "")[:120],
            "topic": w.get("topic", ""), "subject": w.get("subject", ""),
            "count": w.get("count", 0), "last_wrong": w.get("last_wrong", 0),
            "due_at": w.get("due_at", 0),
            "due": w.get("due_at", 0) <= now,
        } for w in wrong[:60]],
        "history": hist[-20:],
    }


def wrong_book(who, now=None):
    """错题本原始明细（导出用）：题干、选项、答案、解析、错次数、复习时间。

    按「最近答错」倒序；due 表示是否到了该复习的时间。
    """
    now = float(now or time.time())
    with _lock:
        dev = dict(_dev(_load(), who, create=False))
    out = []
    for w in dev.get("wrong") or []:
        item = dict(w)
        item["due"] = int(w.get("due_at") or 0) <= now
        item["status"] = "待复习" if item["due"] else (
            "已安排复习" if w.get("due_at") else "待安排")
        out.append(item)
    out.sort(key=lambda x: int(x.get("last_wrong") or 0), reverse=True)
    return out


def summary(who, now=None):
    """答题页/接口用的精简版：不暴露完整错题明细。"""
    a = ability(who, now)
    return {
        "total": a["total"], "accuracy": a["accuracy"],
        "difficulty": a["difficulty"], "target_star": a["target_star"],
        "recent_accuracy": a["recent_accuracy"],
        "wrong_count": a["wrong_count"], "due_count": a["due_count"],
        "weak": [w["topic"] for w in a["weak"][:3]],
    }


# ---------------------------------------------------------------- 组卷

def _weighted_pick(candidates, weights):
    """按权重无放回抽一道题。"""
    if not candidates:
        return None
    total = sum(weights)
    if total <= 0:
        return candidates.pop(random.randrange(len(candidates)))
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return candidates.pop(i)
    return candidates.pop()


def _fix_type_mix(chosen, pool, topic_cap=None, protect=None):
    """尽量让一轮里三种题型都出现：缺哪种就用池里的同类题换掉一道普通题。

    protect 里的题（错题层）不会被换掉——错题优先于题型搭配，
    否则会出现「明明有错题没出，却被换成了随机题型」的情况。
    """
    protect = set(protect or ())
    wanted = [t for t in ("single", "multi", "judge")
              if any(q.get("type") == t for q in pool)]
    if len(wanted) < 2 or len(chosen) < 2:
        return chosen
    have = {q.get("type") for q in chosen}
    ids = {q.get("id") for q in chosen}
    for t in wanted:
        if t in have:
            continue
        candidates = [q for q in pool if q.get("type") == t and q.get("id") not in ids]
        if not candidates:
            continue
        if topic_cap:
            counts = {}
            for q in chosen:
                k = topic_of(q)
                counts[k] = counts.get(k, 0) + 1
            preferred = [c for c in candidates if counts.get(topic_of(c), 0) < topic_cap]
            if preferred:
                candidates = preferred
        # 换掉一道「非唯一题型」的题，避免把刚补进来的又换走；
        # 有 protect（错题）时优先不碰它们
        slots = [i for i, q in enumerate(chosen)
                 if sum(1 for x in chosen if x.get("type") == q.get("type")) > 1
                 and q.get("id") not in protect]
        if not slots:
            slots = [i for i, q in enumerate(chosen)
                     if sum(1 for x in chosen if x.get("type") == q.get("type")) > 1]
        if not slots:
            slots = list(range(len(chosen)))
        chosen[random.choice(slots)] = random.choice(candidates)
        have.add(t)
        ids = {q.get("id") for q in chosen}
    return chosen


def _enforce_topic_cap(chosen, pool, cap):
    """题型补齐可能又把某知识点顶过上限，这里做一次兜底替换。"""
    counts = {}
    for q in chosen:
        k = topic_of(q)
        counts[k] = counts.get(k, 0) + 1
    if not counts or max(counts.values()) <= cap:
        return chosen
    ids = {q.get("id") for q in chosen}
    for i, q in enumerate(chosen):
        k = topic_of(q)
        if counts[k] <= cap:
            continue
        spare = [c for c in pool if c.get("id") not in ids
                 and counts.get(topic_of(c), 0) < cap]
        if not spare:
            continue
        same = [c for c in spare if c.get("type") == q.get("type")]
        rep = random.choice(same or spare)
        counts[k] -= 1
        counts[topic_of(rep)] = counts.get(topic_of(rep), 0) + 1
        ids.discard(q.get("id"))
        ids.add(rep.get("id"))
        chosen[i] = rep
    return chosen


# ---------- 智能取题（3.13.0 四层优先级：错题/新题/毕业错题/已会题）----------

# 3.13.0 四层优先级与比例：错题(未消化) / 新题 / 已毕业的错题 / 已会题
PICK_RATIOS = (0.40, 0.40, 0.10, 0.10)


def _quotas(n, ratios):
    """按比例算各层配额：先向下取整，剩余名额按小数余量从大到小分配
    （余量相同优先靠前的层）。例：n=10 → 4/4/1/1，n=6 → 2/2/1/1，n=1 → 1/0/0/0。"""
    q = [int(n * r) for r in ratios]
    rem = n - sum(q)
    order = sorted(range(len(ratios)),
                   key=lambda i: (-(n * ratios[i] - q[i]), i))
    for i in order[:max(0, rem)]:
        q[i] += 1
    return q


def _star_of(q):
    try:
        return max(0, min(5, int(round(float((q or {}).get("star") or 0)))))
    except (TypeError, ValueError):
        return 0


# 3.15.0：组卷也照顾难度目标——离目标星级越远的题排越后
DIFF_PICK_WEIGHT = 0.6


def _weak_order(qs, weak, target=0):
    """层内排序：弱项知识点优先，其次离目标难度近的题优先（同分随机）。

    这样既练弱项，又尽量让这一轮落在「孩子能做对九成」的难度上。
    没有 star 字段的老题按目标难度处理，不会被区别对待。
    """
    lst = list(qs)
    random.shuffle(lst)
    if target:
        def key(q):
            w = weak.get(topic_of(q))
            # 弱项归一化到 0~1，难度距离最多扣 0.6×4=2.4，
            # 所以「离目标难度太远」能压过「是弱项」，避免一直拿难题练弱项。
            wn = min(1.0, w["score"] / 6.0) if w else 0.0
            dist = abs((_star_of(q) or target) - target)
            return -(wn - DIFF_PICK_WEIGHT * dist)
        lst.sort(key=key)
    else:
        lst.sort(key=lambda q: -(weak[topic_of(q)]["score"]
                                 if topic_of(q) in weak else 0))
    return lst


def pick_questions(pool, who, n, now=None):
    """智能组卷（3.13.0 四层优先级）：

    ① 错题 40% —— 答错过、还没答对过的错题优先消化，复习到期的排最前
    ② 新题 40% —— 没做过的题，保证进度
    ③ 毕业错题 10% —— 以前错过、后来连对两次移出错题本的题，少量复习防遗忘
    ④ 已会题 10% —— 做过且一直答对的题，热身维持
    某层题不够（或被知识点上限挡住）时份额自动顺延给后面的层；
    层内按弱项知识点加权排序。另保留：同一知识点一轮最多占一半、
    题型混合、不重复、乱序。返回 n 道题。
    """
    pool = [q for q in (pool or []) if q]
    n = max(1, min(int(n), len(pool)))
    if not pool:
        return []
    now = float(now or time.time())
    try:
        with _lock:
            dev = dict(_dev(_load(), who, create=False))
    except Exception:
        dev = _empty_device()
    weak = {w["topic"]: w for w in weak_topics(dev)}
    _, target_star = difficulty_of(dev)
    wrong = {str(w.get("qid") or ""): w for w in (dev.get("wrong") or [])}
    seen_wrong, seen_right = set(), set()
    for h in dev.get("history") or []:
        qid = str(h.get("qid") or "")
        if qid:
            (seen_wrong if not h.get("ok") else seen_right).add(qid)

    # 把整个题池分成四层
    tiers = ([], [], [], [])
    for q in pool:
        qid = str(q.get("id") or "")
        if qid and qid in wrong:
            tiers[0].append(q)      # ① 错题：还在错题本里 = 没答对过
        elif qid and qid in seen_wrong:
            tiers[2].append(q)      # ③ 错过但已毕业（连对两次移出错题本）
        elif qid and qid in seen_right:
            tiers[3].append(q)      # ④ 做过且一直对
        else:
            tiers[1].append(q)      # ② 没做过的新题
    # 错题层内部：复习到期的在前，都没到期则最近做错的在前
    tiers[0].sort(
        key=lambda q: (
            wrong.get(str(q.get("id") or ""), {}).get("due_at", 0) <= now,
            wrong.get(str(q.get("id") or ""), {}).get("last_wrong", 0)),
        reverse=True)

    chosen, used, counts = [], set(), {}
    # 弱项要「多出题」但不能整轮只做弱项，孩子会崩；同一知识点最多占半轮
    topic_cap = max(1, int(math.ceil(n * 0.5)))

    def take(cands, k):
        """按给定顺序取 k 道（跳过已用/超知识点上限的），返回实际取到的数量。"""
        got = 0
        for q in cands:
            if got >= k:
                break
            qid = q.get("id")
            if qid in used or counts.get(topic_of(q), 0) >= topic_cap:
                continue
            used.add(qid)
            t = topic_of(q)
            counts[t] = counts.get(t, 0) + 1
            chosen.append(q)
            got += 1
        return got

    # 按四层配额取题；某层不够的份额顺延给后面的层，最后弱项加权补齐
    shortfall = 0
    for tier, quota in zip(tiers, _quotas(n, PICK_RATIOS)):
        shortfall += quota - take(_weak_order(tier, weak, target_star), quota)
    for tier in tiers:
        if shortfall <= 0:
            break
        shortfall -= take(_weak_order(tier, weak, target_star), shortfall)
    while len(chosen) < n:
        remaining = [q for q in pool if q.get("id") not in used]
        if not remaining:
            break
        eligible = [q for q in remaining
                    if counts.get(topic_of(q), 0) < topic_cap]
        if not eligible:
            eligible = remaining
        weights = []
        for q in eligible:
            w = weak.get(topic_of(q))
            wt = 1.0 + 2.0 * min(1.0, w["score"] / 6.0) if w else 1.0
            star = _star_of(q)
            if target_star and star:
                # 兜底补齐时也别光挑难题：离目标难度越远权重越低
                wt *= max(0.15, 1.0 - 0.28 * abs(star - target_star))
            weights.append(wt)
        pick = _weighted_pick(eligible, weights)
        if pick is None:
            break
        used.add(pick.get("id"))
        t = topic_of(pick)
        counts[t] = counts.get(t, 0) + 1
        chosen.append(pick)

    chosen = _fix_type_mix(chosen, pool, topic_cap,
                           protect={q.get("id") for q in tiers[0]})
    chosen = _enforce_topic_cap(chosen, pool, topic_cap)
    # 去重保险（题库里若有重复 id）
    out, seen = [], set()
    for q in chosen:
        key = q.get("id") or q.get("text")
        if key not in seen:
            seen.add(key)
            out.append(q)
    random.shuffle(out)          # 复习题不总排在最前面
    return out[:n]
