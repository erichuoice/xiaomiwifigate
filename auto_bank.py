#!/usr/bin/env python3
"""自动补充新题（3.10.0：每个题库可以设置不同的自动出题要求）。

3.8.0 按「没做过的题少于阈值」触发；3.10.0 升级为多规则：
- ``state/auto_bank.json`` 里保存一个规则列表，每个题库一条规则，
  阈值、冷却、学段年级、科目、题型配比、最多保留自动题等都各配各的。
- 守护线程逐库检查：哪个题库的新题少于它自己的阈值就单独补哪个，
  互不影响（例：小学库补四年级数学，初中库补八年级物理英语）。
- 旧版单份配置文件（平铺 dict）首次读取时自动包成一条规则，无缝升级。

设计约束（沿用 3.8.0/3.9.0）：
- 「做过」= 这道题曾被任何设备答过（学情 history 里出现过它的 id）。
- 冷却：每个题库独立计时，两次自动执行至少间隔 cooldown_minutes，
  防止生成失败时每 30 秒空转轰炸一次接口。
- 配置与运行记录只写 ``state/auto_bank.json``（权限 600），不进 GitHub、
  不进题库导入导出；``config.json`` / ``runtime.json`` 都不改。
- 触发由 server.py 的守护线程负责；本模块不 import server，避免循环依赖。
- 所有规则共用一把非阻塞执行锁，同一时刻只生成一批，不会并发写题库。
"""

import copy
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import ai_bank
import learning

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
PATH = os.path.join(STATE, "auto_bank.json")

# 中国全境统一 UTC+8、无夏令时，固定偏移比依赖 tzdata 更稳（alpine 镜像里没有）
TZ = timezone(timedelta(hours=8))
MAX_RUNS = 20
MERGED_RUNS = 30          # 状态卡里合并展示的最近运行记录条数
AUTO_PREFIX = "auto-"
MODES = ("auto", "offline", "ai")
DOC_VERSION = 2

# 单条规则的默认值（每个题库一条，未设置的题库用这份兜底）
DEFAULTS = {
    # 默认开启：在后台选中某个题库保存后即生效，无需再点别的开关
    "enabled": True,
    "threshold": 30,           # 没做过的题少于多少道时触发
    "cooldown_minutes": 30,   # 两次自动执行的最小间隔（分钟）
    "count": 30,               # 每次最多生成 1~30 道
    "bank_id": "",             # 空 = 第一个题库（保存时会落成具体 id）
    "stage": "小学",
    "grade": "四年级",
    "subject": "数学",           # 兼容旧配置：单科目字符串
    "subjects": ["数学"],        # 3.9.0：可多选的科目列表（为准）
    "topics": "",
    "mode": "auto",             # auto=优先 AI 失败降级 / offline / ai=不降级
    "mix": {},                  # 空 = 自动按比例分配三种题型
    "device_mac": "",           # 非空 = 针对这台设备的弱项与错题出题
    "max_auto": 60,             # 该题库最多保留多少道「自动题」，0 = 不限
    "last_run": 0,
    "next_run": 0,              # = 下次允许自动执行的时间（冷却结束时刻）
    "runs": [],
}


class AutoBankError(Exception):
    pass


_run_lock = threading.Lock()      # 单次执行互斥（所有规则共用）
_file_lock = threading.Lock()     # 配置文件读写


# ---------------------------------------------------------------- 工具

def fmt_ts(ts, with_time=True):
    """时间戳 → 广州时间的可读字符串。"""
    try:
        ts = float(ts or 0)
        if ts <= 0:
            return "—"
        fmt = "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d"
        return datetime.fromtimestamp(ts, TZ).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return "—"


def relative(ts, now=None):
    now = float(now if now is not None else time.time())
    diff = float(ts or 0) - now
    if not ts:
        return "未安排"
    if diff <= 0:
        return "冷却已结束，可随时触发"
    if diff < 3600:
        return "约 %d 分钟后" % max(1, int(diff / 60))
    if diff < 48 * 3600:
        return "约 %.1f 小时后" % (diff / 3600)
    return "约 %d 天后" % int(diff / 86400)


def _int(value, low, high, label, default=0):
    try:
        out = int(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise AutoBankError("%s 必须是整数" % label)
    if not low <= out <= high:
        raise AutoBankError("%s 需在 %s—%s 之间" % (label, low, high))
    return out


def normalize(raw, base=None):
    """把任意表单/文件内容收敛成一条安全规则。"""
    src = dict(DEFAULTS)
    if base:
        src.update({k: v for k, v in base.items() if k in DEFAULTS})
    if isinstance(raw, dict):
        src.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out = copy.deepcopy(src)

    out["enabled"] = bool(src.get("enabled"))
    out["threshold"] = _int(src.get("threshold"), 5, 500, "触发阈值", 30)
    out["cooldown_minutes"] = _int(src.get("cooldown_minutes"), 1, 1440,
                                   "冷却分钟", 30)
    out["count"] = _int(src.get("count"), 1, 30, "每次题量", 30)
    out["max_auto"] = _int(src.get("max_auto"), 0, 5000, "最多保留自动题", 60)
    out["bank_id"] = str(src.get("bank_id") or "").strip()[:64]
    out["stage"] = str(src.get("stage") or "").strip()[:16] or "小学"
    out["grade"] = str(src.get("grade") or "").strip()[:16]
    # 科目：本次请求里显式给了就以它为准（subjects 优先），否则沿用已保存的配置。
    # 不能直接读 src：DEFAULTS 里带着 subjects 默认值，会把旧的单科目配置盖掉。
    picked = None
    if isinstance(raw, dict):
        picked = raw.get("subjects") or raw.get("subject")
    if not picked:
        picked = src.get("subjects") or src.get("subject")
    out["subjects"] = ai_bank.normalize_subjects(picked, default=("数学",))
    out["subject"] = ("、".join(out["subjects"]))[:64]
    out["topics"] = str(src.get("topics") or "").strip()[:200]
    mode = str(src.get("mode") or "auto").strip()
    if mode not in MODES:
        raise AutoBankError("未知的出题方式")
    out["mode"] = mode
    out["device_mac"] = str(src.get("device_mac") or "").strip().lower()[:24]
    mix = {}
    raw_mix = src.get("mix") if isinstance(src.get("mix"), dict) else {}
    for k in ("single", "multi", "judge"):
        v = raw_mix.get(k)
        if v in (None, ""):
            continue
        mix[k] = _int(v, 0, 30, "题型数量", 0)
    if mix and sum(mix.values()) > 30:
        raise AutoBankError("三种题型的数量相加不能超过 30")
    out["mix"] = mix
    out["last_run"] = float(src.get("last_run") or 0)
    out["next_run"] = float(src.get("next_run") or 0)
    runs = src.get("runs") if isinstance(src.get("runs"), list) else []
    out["runs"] = [r for r in runs if isinstance(r, dict)][:MAX_RUNS]
    return out


# ---------------------------------------------------------------- 读写

def _write(doc):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass


def _read_doc():
    """读原始文档；旧版（3.9.0 及以前）的单份平铺配置自动包成一条规则。"""
    with _file_lock:
        raw = {}
        if os.path.exists(PATH):
            try:
                with open(PATH, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if isinstance(raw.get("rules"), list):
            return {"version": DOC_VERSION, "rules": raw["rules"]}
        # 旧格式：整个文件就是一份配置 → 迁移成规则列表（保留原有开关与计时）
        return {"version": DOC_VERSION, "rules": [raw] if raw else []}


def _save_doc(doc):
    with _file_lock:
        _write({"version": DOC_VERSION, "rules": doc.get("rules") or []})


def first_bank_id(st):
    try:
        return str((st.first_bank() or {}).get("id") or "")
    except Exception:
        return ""


def resolve_bank_id(st, bank_id):
    """空题库 id = 第一个题库；返回具体 id，找不到时报错。"""
    bank_id = str(bank_id or "").strip()
    if not bank_id:
        bank_id = first_bank_id(st)
    if not bank_id or not st.bank(bank_id):
        raise AutoBankError("目标题库不存在，请重新选择一个题库")
    return bank_id


def load_all(st=None):
    """读出所有规则并归一化。

    - 旧格式文件自动迁移（_read_doc 已处理）；
    - bank_id 为空（=第一个题库）的规则落成具体 id 并写回，之后全程用具体 id，
      避免同一题库出现两条规则；
    - 同一个 bank_id 只保留一条（后写的优先）。
    """
    doc = _read_doc()
    rules, changed, seen = [], False, {}
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict):
            changed = True
            continue
        try:
            rule = normalize(raw)
        except AutoBankError:
            changed = True
            continue
        if not rule["bank_id"] and st is not None:
            first = first_bank_id(st)
            if first:
                rule["bank_id"] = first
        if rule["bank_id"] in seen:
            changed = True            # 同库重复：只留一条
        seen[rule["bank_id"]] = rule
    rules = list(seen.values())
    if st is not None:
        for r in rules:
            if not str(r.get("bank_id") or "").strip():
                changed = True
    if changed or [r["bank_id"] for r in rules] != \
            [str((x or {}).get("bank_id") or "") for x in doc.get("rules") or []]:
        _save_doc({"version": DOC_VERSION, "rules": rules})
    return rules


def load_rule(st, bank_id):
    """取某个题库的规则；没设置过返回 None。"""
    want = str(bank_id or "").strip()
    for r in load_all(st):
        if r["bank_id"] == want:
            return r
    return None


def save_rule(patch, st=None, now=None):
    """保存（新增或覆盖）某个题库的规则。

    阈值/冷却/开关变了才重排冷却，改题量等不动计时；last_run/runs 沿用旧值。
    """
    now = float(now if now is not None else time.time())
    body = dict(patch) if isinstance(patch, dict) else {}
    if st is not None:
        body["bank_id"] = resolve_bank_id(st, body.get("bank_id"))
    bank_id = str(body.get("bank_id") or "").strip()
    if not bank_id:
        raise AutoBankError("请先选择题库")
    doc = _read_doc()
    first = first_bank_id(st) if st is not None else ""
    rules = []
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("bank_id") or "").strip()
        if not rid and first:        # 旧版空题库 id = 第一个题库
            raw = dict(raw, bank_id=first)
            rid = first
        if rid:
            rules.append(raw)
    old = None
    for raw in rules:
        if str(raw.get("bank_id") or "") == bank_id:
            old = raw
            break
    clean = normalize(body, old)
    clean["bank_id"] = bank_id
    if old:
        clean["last_run"] = float(old.get("last_run") or 0)
        runs = old.get("runs") if isinstance(old.get("runs"), list) else []
        clean["runs"] = [r for r in runs if isinstance(r, dict)][:MAX_RUNS]
    # 冷却编排（与 3.8.0 相同）
    if not clean["enabled"]:
        clean["next_run"] = 0.0
    elif old is None or not float(old.get("next_run") or 0) or any(
            clean[k] != old.get(k) for k in
            ("enabled", "threshold", "cooldown_minutes")):
        clean["next_run"] = cooldown_until(clean, now)
    else:
        clean["next_run"] = float(old.get("next_run") or 0)
    rules = [r for r in rules if str(r.get("bank_id") or "") != bank_id]
    rules.append(clean)
    _save_doc({"version": DOC_VERSION, "rules": rules})
    return clean


def remove_rule(bank_id):
    """删除某个题库的自动出题规则（已入库的题不受影响）。"""
    bank_id = str(bank_id or "").strip()
    if not bank_id:
        raise AutoBankError("请先选择题库")
    doc = _read_doc()
    rules = [r for r in doc.get("rules") or []
             if str((r or {}).get("bank_id") or "").strip() != bank_id]
    _save_doc({"version": DOC_VERSION, "rules": rules})


def clear_runs(bank_id=None):
    """清空运行记录；给 bank_id 时只清这个题库的。"""
    bank_id = str(bank_id or "").strip()
    doc = _read_doc()
    rules = [r if isinstance(r, dict) else {} for r in doc.get("rules") or []]
    for r in rules:
        if not bank_id or str(r.get("bank_id") or "") == bank_id:
            r["runs"] = []
    _save_doc({"version": DOC_VERSION, "rules": rules})


def cooldown_until(cfg, now=None):
    """从 now 起算的冷却结束时刻；next_run 只用来表达「最早可再执行时间」。"""
    now = float(now if now is not None else time.time())
    if not cfg.get("enabled"):
        return 0.0
    return now + cfg["cooldown_minutes"] * 60


# ---------------------------------------------------------------- 触发判定

def fresh_questions(bank, done=None):
    """题库里还没被做过的题（学情 history 里没有它的 id）。"""
    done = done if done is not None else learning.done_qids()
    return [q for q in bank.get("questions") or [] if q.get("id") not in done]


def fresh_count(st, cfg, done=None):
    """目标题库里「没做过」的题数。"""
    bank_id = cfg["bank_id"] or first_bank_id(st)
    bank = st.bank(bank_id) if bank_id else None
    if not bank:
        return 0
    return len(fresh_questions(bank, done))


def due(st, cfg, now=None):
    """满足两个条件才算到点：新题少于该库阈值，且该库冷却已结束。"""
    now = float(now if now is not None else time.time())
    if not cfg.get("enabled"):
        return False
    if fresh_count(st, cfg) >= cfg["threshold"]:
        return False
    return now >= cfg.get("next_run", 0)


def _bank_status(st, bank, rule, done, now):
    """单个题库的状态行。"""
    fresh = len(fresh_questions(bank, done))
    auto_count = sum(1 for q in bank["questions"]
                     if str(q.get("source") or "").startswith(AUTO_PREFIX))
    threshold = rule["threshold"] if rule else 0
    if not rule:
        state_text = "未设置"
        next_text = "未设置：在下方选中该题库并保存即可启用"
        cooldown_until_text = cooldown_relative = "—"
        last_run_text = "—"
        subjects = []
    elif not rule["enabled"]:
        state_text = "已设置，未启用"
        next_text = "未启用"
        cooldown_until_text = cooldown_relative = "—"
        last_run_text = fmt_ts(rule["last_run"])
        subjects = rule["subjects"]
    else:
        state_text = "已启用"
        if fresh < rule["threshold"]:
            next_text = "将生成 %d 道，%s" % (
                rule["count"],
                "冷却中（%s）" % fmt_ts(rule["next_run"])
                if now < rule["next_run"] else "等待守护线程执行")
        else:
            next_text = "新题充足，答到低于阈值就会触发"
        cooldown_until_text = fmt_ts(rule["next_run"])
        cooldown_relative = relative(rule["next_run"], now)
        last_run_text = fmt_ts(rule["last_run"])
        subjects = rule["subjects"]
    return {
        "bank_id": bank["id"],
        "bank_name": bank["name"],
        "bank_total": len(bank["questions"]),
        "auto_questions": auto_count,
        "configured": bool(rule),
        "enabled": bool(rule and rule["enabled"]),
        "state_text": state_text,
        "fresh": fresh,
        "threshold": threshold,
        "need": max(0, threshold - fresh) if rule else 0,
        "count": rule["count"] if rule else 0,
        "cooldown_minutes": rule["cooldown_minutes"] if rule else 0,
        "cooldown_until": rule["next_run"] if (rule and rule["enabled"]) else 0,
        "cooldown_until_text": cooldown_until_text,
        "cooldown_relative": cooldown_relative,
        "next_text": next_text,
        "last_run_text": last_run_text,
        "subjects": subjects,
        "runs": (rule or {}).get("runs") or [],
    }


def status(st, now=None):
    """后台状态卡：每个题库一行状态 + 合并的最近运行记录。"""
    now = float(now if now is not None else time.time())
    rules = {r["bank_id"]: r for r in load_all(st)}
    done = learning.done_qids()
    banks = [b for b in st.banks() if isinstance(b, dict)]
    rows = [_bank_status(st, b, rules.get(b["id"]), done, now) for b in banks]
    merged = []
    for r in rules.values():
        merged.extend((r.get("runs") or [])[:MERGED_RUNS])
    merged.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    try:
        has_key = bool(ai_bank.public_config().get("has_key"))
    except Exception:
        has_key = False
    return {
        "server_now": now,
        "now_text": fmt_ts(now),
        "running": _run_lock.locked(),
        "has_key": has_key,
        "banks": rows,
        "runs": merged[:MERGED_RUNS],
    }


# ---------------------------------------------------------------- 单次执行

def _mark(qs, source):
    """自动题打上来源标记，方便按上限淘汰时与内置题/人工题区分。"""
    out = []
    for q in qs:
        item = dict(q)
        item["source"] = (AUTO_PREFIX + str(source or "offline"))[:16]
        out.append(item)
    return out


def run_once(st, rule, reason="auto", now=None):
    """按某条规则生成一批新题并入库到它指定的题库。

    无论成败都进入该库自己的冷却并落一条记录。
    自动触发（reason=auto）与手动试跑都按「每次生成数量」出满一批（3.14.4 起：
    不再只补缺口，避免阈值 30、只有 29 道新题时只生成 1 道）。
    """
    if not _run_lock.acquire(blocking=False):
        raise AutoBankError("上一次自动出题还在执行，请稍候再试")
    now = float(now if now is not None else time.time())
    t0 = time.time()
    bank_id = str(rule.get("bank_id") or "").strip() or first_bank_id(st)
    rec = {
        "at": now, "at_text": fmt_ts(now), "reason": reason, "bank_id": bank_id,
        "ok": False, "generated": 0, "added": 0, "skipped": 0,
        "total": 0, "pruned": 0, "fresh": 0, "source": "", "model": "",
        "bank_name": "", "latency": 0, "detail": "",
    }
    try:
        bank = st.bank(bank_id) if bank_id else None
        if not bank:
            raise AutoBankError("目标题库不存在，请在设置里重新选择一个题库")
        rec["bank_name"] = bank["name"]
        cfg = dict(rule)
        cfg["bank_id"] = bank_id

        count = cfg["count"]
        if reason != "manual":
            fresh = fresh_count(st, cfg)
            if fresh >= cfg["threshold"]:
                raise AutoBankError("新题已够（%s 道未做过 ≥ 阈值 %s），无需补充"
                                    % (fresh, cfg["threshold"]))

        spec = {
            "stage": cfg["stage"], "grade": cfg["grade"],
            "subject": cfg["subject"], "subjects": cfg["subjects"], "count": count,
            "topics": cfg["topics"], "mode": cfg["mode"],
            "avoid": [q["text"] for q in bank["questions"]][:600],
        }
        if cfg["mix"]:
            wanted = sum(cfg["mix"].values())
            if wanted and wanted < count:
                # 题型配比是「至少」数量：按比例扩到本次要出的总题量
                scale = count / wanted
                spec["mix"] = {k: max(1, round(v * scale))
                               for k, v in cfg["mix"].items()}
            else:
                spec["mix"] = cfg["mix"]
        if cfg["device_mac"]:
            result = ai_bank.generate_for_device(
                spec, learning.ability(cfg["device_mac"]))
        else:
            result = ai_bank.generate(spec)

        rec["source"] = result.get("source", "")
        rec["model"] = result.get("model", "")
        qs = _mark(result.get("questions") or [], rec["source"])
        rec["generated"] = len(qs)
        if not qs:
            raise AutoBankError("这次没有生成可用的新题（可能题目都重复了）")
        saved = st.apply_questions(bank_id, qs, mode="append")
        rec["added"] = saved["added"]
        rec["skipped"] = saved["skipped"]
        rec["total"] = saved["total"]
        if cfg["max_auto"]:
            rec["pruned"] = st.prune_auto_questions(bank_id, cfg["max_auto"])
            rec["total"] = len(st.bank(bank_id)["questions"])
        rec["fresh"] = fresh_count(st, cfg)
        rec["ok"] = True
        rec["detail"] = result.get("note", "") or "已完成"
    except Exception as exc:  # 出题失败不能影响答题/放行主流程
        rec["detail"] = str(exc)
        try:
            rec["fresh"] = fresh_count(st, {"bank_id": bank_id})
        except Exception:
            pass
    finally:
        rec["latency"] = round(time.time() - t0, 1)
        try:
            doc = _read_doc()
            for raw in doc.get("rules") or []:
                if not isinstance(raw, dict):
                    continue
                rid = str(raw.get("bank_id") or "").strip()
                if rid == bank_id or (not rid and
                                      bank_id == first_bank_id(st)):
                    try:
                        latest = normalize(raw)
                    except AutoBankError:
                        continue
                    latest["last_run"] = now
                    latest["next_run"] = (cooldown_until(latest, now)
                                          if latest["enabled"] else 0.0)
                    latest["runs"] = ([rec] + latest["runs"])[:MAX_RUNS]
                    raw.clear()
                    raw.update(latest)
                    break
            _save_doc(doc)
        except Exception:
            pass
        _run_lock.release()
    return rec
