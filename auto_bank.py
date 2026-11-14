#!/usr/bin/env python3
"""自动补充新题（3.8.0）：题库里「没做过」的题少于阈值时自动生成补足。

3.7.0 是按时间表定时出题；3.8.0 按用户要求改为按剩余新题数触发：
- 「做过」= 这道题曾被任何设备答过（学情 history 里出现过它的 id）。
- 「没做过的题」（新题）少于阈值（默认 30）时，自动生成一批新题直接入库，
  把新题缺口补到阈值；生成仍会避开题库已有题干，不会重复。
- 冷却：两次自动执行至少间隔 cooldown_minutes（默认 30 分钟），
  防止生成失败时每 30 秒空转轰炸一次接口。

设计约束（沿用 3.7.0）：
- 配置与运行记录只写 ``state/auto_bank.json``（权限 600），不进 GitHub、
  不进题库导入导出；``config.json`` / ``runtime.json`` 都不改。
- 触发由 server.py 的守护线程负责；本模块不 import server，避免循环依赖。
- 自动触发和后台「立即试跑」共用一把非阻塞锁，同一时刻只跑一次。
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
AUTO_PREFIX = "auto-"
MODES = ("auto", "offline", "ai")

DEFAULTS = {
    # 默认开启：升级后无需人工点任何开关，题不够就自动补（后台可随时关闭）
    "enabled": True,
    "threshold": 30,           # 没做过的题少于多少道时触发
    "cooldown_minutes": 30,   # 两次自动执行的最小间隔（分钟）
    "count": 30,               # 每次最多生成 1~30 道
    "bank_id": "",             # 空 = 第一个题库
    "stage": "小学",
    "grade": "四年级",
    "subject": "数学",
    "topics": "",
    "mode": "auto",             # auto=优先 AI 失败降级 / offline / ai=不降级
    "mix": {},                  # 空 = 自动按比例分配三种题型
    "device_mac": "",           # 非空 = 针对这台设备的弱项与错题出题
    "max_auto": 60,             # 该题库最多保留多少道「自动题」，0 = 不限
    "last_run": 0,
    "next_run": 0,              # 3.8.0：= 下次允许自动执行的时间（冷却结束时刻）
    "runs": [],
}


class AutoBankError(Exception):
    pass


_run_lock = threading.Lock()      # 单次执行互斥
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
    """把任意表单/文件内容收敛成一份安全配置。"""
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
    out["subject"] = str(src.get("subject") or "").strip()[:16] or "数学"
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

def _write(cfg):
    os.makedirs(STATE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass


def load():
    with _file_lock:
        raw = {}
        if os.path.exists(PATH):
            try:
                with open(PATH, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
        try:
            cfg = normalize(raw)
        except AutoBankError:
            cfg = copy.deepcopy(DEFAULTS)
        return cfg


def _save_cfg(cfg):
    with _file_lock:
        _write(cfg)


def save(patch, now=None):
    """保存后台表单。阈值/冷却/开关变了才重排冷却，改题量等不动计时。"""
    cfg = load()
    clean = normalize(patch, cfg)
    clean["last_run"] = cfg["last_run"]
    clean["runs"] = cfg["runs"]
    now = float(now if now is not None else time.time())
    rule_changed = any(clean[k] != cfg[k] for k in
                       ("enabled", "threshold", "cooldown_minutes"))
    if not clean["enabled"]:
        clean["next_run"] = 0.0
    elif rule_changed or not cfg["next_run"]:
        clean["next_run"] = cooldown_until(clean, now)
    else:
        clean["next_run"] = cfg["next_run"]
    _save_cfg(clean)
    return clean


def clear_runs():
    cfg = load()
    cfg["runs"] = []
    _save_cfg(cfg)
    return cfg


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
    bank_id = cfg["bank_id"] or (st.first_bank() or {}).get("id", "")
    bank = st.bank(bank_id) if bank_id else None
    if not bank:
        return 0
    return len(fresh_questions(bank, done))


def due(st, cfg, now=None):
    """满足两个条件才算到点：新题少于阈值，且冷却已结束。"""
    now = float(now if now is not None else time.time())
    if not cfg.get("enabled"):
        return False
    if fresh_count(st, cfg) >= cfg["threshold"]:
        return False
    return now >= cfg.get("next_run", 0)


def status(st, now=None):
    """后台状态卡：新题数/阈值、冷却、题库里的自动题数、是否已配 Key。"""
    now = float(now if now is not None else time.time())
    cfg = load()
    bank_id = cfg["bank_id"] or (st.first_bank() or {}).get("id", "")
    bank = st.bank(bank_id) if bank_id else None
    auto_count = 0
    if bank:
        auto_count = sum(1 for q in bank["questions"]
                         if str(q.get("source") or "").startswith(AUTO_PREFIX))
    try:
        has_key = bool(ai_bank.public_config().get("has_key"))
    except Exception:
        has_key = False
    if cfg["enabled"]:
        if fresh_count(st, cfg) < cfg["threshold"]:
            next_text = "满足触发条件，%s" % (
                "冷却中（%s）" % fmt_ts(cfg["next_run"])
                if now < cfg["next_run"] else "等待守护线程执行")
        else:
            next_text = "不满足（新题足够），一旦答题消耗到低于阈值就会触发"
    else:
        next_text = "未启用"
    return {
        "server_now": now,
        "now_text": fmt_ts(now),
        "running": _run_lock.locked(),
        "enabled": cfg["enabled"],
        "fresh": fresh_count(st, cfg),
        "threshold": cfg["threshold"],
        "need": max(0, cfg["threshold"] - fresh_count(st, cfg)),
        "cooldown_minutes": cfg["cooldown_minutes"],
        "cooldown_until": cfg["next_run"] if cfg["enabled"] else 0,
        "cooldown_until_text": fmt_ts(cfg["next_run"]) if cfg["enabled"] else "—",
        "cooldown_relative": relative(cfg["next_run"], now) if cfg["enabled"] else "—",
        "next_text": next_text,
        "last_run": cfg["last_run"],
        "last_run_text": fmt_ts(cfg["last_run"]),
        "bank_id": bank_id,
        "bank_name": bank["name"] if bank else "（题库不存在）",
        "bank_total": len(bank["questions"]) if bank else 0,
        "auto_questions": auto_count,
        "has_key": has_key,
        "runs": cfg["runs"],
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


def run_once(st, reason="auto", now=None):
    """生成一批新题并入库；无论成败都进入冷却并落一条记录。

    自动触发（reason=auto）时只补缺口：缺 8 道就生成 8 道，避免题库猛涨；
    手动试跑（reason=manual）按表单题量出满一批。
    """
    if not _run_lock.acquire(blocking=False):
        raise AutoBankError("上一次自动出题还在执行，请稍候再试")
    now = float(now if now is not None else time.time())
    t0 = time.time()
    rec = {
        "at": now, "at_text": fmt_ts(now), "reason": reason,
        "ok": False, "generated": 0, "added": 0, "skipped": 0,
        "total": 0, "pruned": 0, "fresh": 0, "source": "", "model": "",
        "bank_name": "", "latency": 0, "detail": "",
    }
    cfg = load()
    try:
        bank_id = cfg["bank_id"] or (st.first_bank() or {}).get("id", "")
        bank = st.bank(bank_id) if bank_id else None
        if not bank:
            raise AutoBankError("目标题库不存在，请在设置里重新选择一个题库")
        rec["bank_name"] = bank["name"]

        count = cfg["count"]
        if reason != "manual":
            need = max(0, cfg["threshold"] - fresh_count(st, cfg))
            count = min(count, max(1, need))
            if need <= 0:
                raise AutoBankError("新题已够（%s 道未做过 ≥ 阈值 %s），无需补充"
                                    % (fresh_count(st, cfg), cfg["threshold"]))

        spec = {
            "stage": cfg["stage"], "grade": cfg["grade"],
            "subject": cfg["subject"], "count": count,
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
            rec["fresh"] = fresh_count(st, cfg)
        except Exception:
            pass
    finally:
        rec["latency"] = round(time.time() - t0, 1)
        try:
            latest = load()
            latest["last_run"] = now
            latest["next_run"] = cooldown_until(latest, now) if latest["enabled"] else 0.0
            latest["runs"] = ([rec] + latest["runs"])[:MAX_RUNS]
            _save_cfg(latest)
        except Exception:
            pass
        _run_lock.release()
    return rec
