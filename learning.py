#!/usr/bin/env python3
"""学情模型：错题本、知识点掌握度、弱项识别与智能组卷。

数据落在 state/learning.json（不入库、不随代码更新覆盖）：
  devices[mac] = {
    history: 最近若干次逐题作答记录,
    wrong:   错题本（含题目快照、知识点、复习到期时间）,
    topics:  每个知识点的对错次数与 EWMA 掌握度,
    rounds:  答题轮次统计,
  }

组卷不再是纯随机（3.13.0 起按四层优先级取题）：
  1) 错题（答错过、还没答对过的）优先，占约 40%——复习到期的先出
  2) 没做过的新题占约 40%，保证进度推进
  3) 以前错过、后来连对「毕业」的题占约 10%，少量复习防遗忘
  4) 做过且一直答对的已会题占约 10%，热身维持
  某层题不够时份额自动顺延给后面的层；层内仍按弱项知识点加权。
  另保留：弱项加权（层内排序）、同一知识点一轮最多占一半、
  尽量三种题型都出现、一轮之内不重复、乱序出题。
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


def _load():
    if not os.path.exists(PATH):
        return {"version": 1, "devices": {}}
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        return {"version": 1, "devices": {}}


def _save(data):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)


def _dev(data, mac, create=True):
    mac = (mac or "").lower()
    dev = data["devices"].get(mac)
    if not dev and create:
        dev = _empty_device()
        data["devices"][mac] = dev
    if dev:
        dev.setdefault("history", [])
        dev.setdefault("wrong", [])
        dev.setdefault("topics", {})
        dev.setdefault("rounds", {"total": 0, "perfect": 0, "last": 0})
    return dev or _empty_device()


# ---------------------------------------------------------------- 记录

def record_answer(mac, q, ok, ts=None):
    """每答一题记一次：知识点掌握度、历史、错题本与复习排期。"""
    mac = (mac or "").lower()
    if not mac:
        return
    ts = float(ts or time.time())
    topic = topic_of(q)
    with _lock:
        data = _load()
        dev = _dev(data, mac)
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


def record_round(mac, all_ok, ts=None):
    mac = (mac or "").lower()
    if not mac:
        return
    with _lock:
        data = _load()
        dev = _dev(data, mac)
        r = dev["rounds"]
        r["total"] = int(r.get("total", 0)) + 1
        r["perfect"] = int(r.get("perfect", 0)) + (1 if all_ok else 0)
        r["last"] = int(ts or time.time())
        _save(data)


def clear(mac):
    """清空某台设备的学情（不影响题库和放行记录）。"""
    with _lock:
        data = _load()
        if (mac or "").lower() in data["devices"]:
            data["devices"].pop((mac or "").lower())
            _save(data)
            return True
    return False


def all_devices():
    with _lock:
        return sorted(_load()["devices"].keys())


def done_qids():
    """所有设备「做过」的题目 id 集合（history + 错题本）。

    供自动出题判断题库里还剩多少没做过的新题；
    history 每台设备最多记最近 400 条，足够覆盖日常使用。
    """
    with _lock:
        data = _load()
    done = set()
    for dev in (data.get("devices") or {}).values():
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


def weak_topics_for(mac, limit=3):
    """供组卷使用：返回该设备当前最薄弱的知识点名称列表。"""
    with _lock:
        dev = _dev(_load(), mac, create=False)
    return [w["topic"] for w in weak_topics(dev, limit)]


def ability(mac, now=None):
    """后台「学情」页用的汇总。"""
    now = float(now or time.time())
    with _lock:
        dev = dict(_dev(_load(), mac, create=False))
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
    return {
        "mac": (mac or "").lower(),
        "total": len(hist), "correct": correct,
        "accuracy": round(correct / len(hist), 3) if hist else None,
        "rounds": int(rounds.get("total", 0)),
        "perfect": int(rounds.get("perfect", 0)),
        "last": int(rounds.get("last", 0)),
        "wrong_count": len(wrong),
        "due_count": sum(1 for w in wrong if w.get("due_at", 0) <= now),
        "weak": weak_topics(dev),
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


def summary(mac, now=None):
    """答题页/接口用的精简版：不暴露完整错题明细。"""
    a = ability(mac, now)
    return {
        "total": a["total"], "accuracy": a["accuracy"],
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


def _fix_type_mix(chosen, pool, topic_cap=None):
    """尽量让一轮里三种题型都出现：缺哪种就用池里的同类题换掉一道普通题。"""
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
        # 换掉一道「非唯一题型」的题，避免把刚补进来的又换走
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


def _weak_order(qs, weak):
    """层内排序：弱项知识点的题排前面（同分随机），供按配额顺序取题。"""
    lst = list(qs)
    random.shuffle(lst)
    lst.sort(key=lambda q: -(weak[topic_of(q)]["score"]
                             if topic_of(q) in weak else 0))
    return lst


def pick_questions(pool, mac, n, now=None):
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
            dev = dict(_dev(_load(), mac, create=False))
    except Exception:
        dev = _empty_device()
    weak = {w["topic"]: w for w in weak_topics(dev)}
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
        shortfall += quota - take(_weak_order(tier, weak), quota)
    for tier in tiers:
        if shortfall <= 0:
            break
        shortfall -= take(_weak_order(tier, weak), shortfall)
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
            weights.append(1.0 + 2.0 * min(1.0, w["score"] / 6.0) if w else 1.0)
        pick = _weighted_pick(eligible, weights)
        if pick is None:
            break
        used.add(pick.get("id"))
        t = topic_of(pick)
        counts[t] = counts.get(t, 0) + 1
        chosen.append(pick)

    chosen = _fix_type_mix(chosen, pool, topic_cap)
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
