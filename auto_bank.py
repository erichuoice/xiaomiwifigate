#!/usr/bin/env python3
"""定时自动出题（3.7.0）：无人值守地定期生成一批新题并自动入库。

设计约束：
- 配置与运行记录只写 ``state/auto_bank.json``（权限 600），不进 GitHub、
  不进题库导入导出；``config.json`` / ``runtime.json`` 都不改。
- 每次执行只做一件事：生成 → 按题干去重追加到指定题库 → 按上限淘汰最旧的自动题
  → 记一条运行记录 → 推进下次执行时间。**失败也推进**，避免接口异常时反复轰炸。
- 服务停机期间不补跑：启动时发现下次时间已过期，只跑一次，然后按当前时间
  重新排下一次，不会把停机期间欠的次数一次性补上。
- 定时触发由 server.py 的守护线程负责；本模块不 import server，避免循环依赖。
- 「定时」和后台「立即试跑」共用一把非阻塞锁，同一时刻只跑一次。
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
FREQUENCIES = ("daily", "interval")
MODES = ("auto", "offline", "ai")

DEFAULTS = {
    # 默认开启：升级后无需人工点任何开关，系统自己按时出题入库（后台可随时关闭）
    "enabled": True,
    "frequency": "daily",       # daily=每天固定时间 / interval=每隔 N 小时
    "time": "03:00",            # 广州时间 HH:MM
    "interval_hours": 24,
    "count": 10,                # 每次生成 1~30 道
    "bank_id": "",              # 空 = 第一个题库
    "stage": "小学",
    "grade": "四年级",
    "subject": "数学",
    "topics": "",
    "mode": "auto",             # auto=优先 AI 失败降级 / offline / ai=不降级
    "mix": {},                  # 空 = 自动按比例分配三种题型
    "device_mac": "",           # 非空 = 针对这台设备的弱项与错题出题
    "max_auto": 60,             # 该题库最多保留多少道「自动题」，0 = 不限
    "last_run": 0,
    "next_run": 0,
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
        return "已到期，等待执行"
    if diff < 3600:
        return "约 %d 分钟后" % max(1, int(diff / 60))
    if diff < 48 * 3600:
        return "约 %.1f 小时后" % (diff / 3600)
    return "约 %d 天后" % int(diff / 86400)


def parse_hhmm(text):
    raw = str(text or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise AutoBankError("时间要写成 03:00 这样的格式")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise AutoBankError("时间要写成 03:00 这样的格式")
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        raise AutoBankError("时间需在 00:00—23:59 之间")
    return h, m


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
    freq = str(src.get("frequency") or "daily").strip()
    if freq not in FREQUENCIES:
        raise AutoBankError("未知的执行频率")
    out["frequency"] = freq
    h, m = parse_hhmm(src.get("time") or "03:00")
    out["time"] = "%02d:%02d" % (h, m)
    out["interval_hours"] = _int(src.get("interval_hours"), 1, 720, "间隔小时", 24)
    out["count"] = _int(src.get("count"), 1, 30, "每次题量", 10)
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
        # 首次运行（或配置损坏重建）时排好下一次，之后完全无人值守；
        # 不设成立即执行，避免服务一启动就往题库里灌题。
        if cfg["enabled"] and not cfg["next_run"]:
            cfg["next_run"] = next_run_at(cfg)
            _write(cfg)          # 已在 _file_lock 内，直接写，勿调 _save_cfg
        return cfg


def _save_cfg(cfg):
    with _file_lock:
        _write(cfg)


def next_run_at(cfg, now=None):
    """按频率算下一次执行时刻；停机期间不补跑，一律从现在往后排。"""
    now = float(now if now is not None else time.time())
    if not cfg.get("enabled"):
        return 0.0
    if cfg.get("frequency") == "interval":
        return now + cfg["interval_hours"] * 3600
    h, m = parse_hhmm(cfg.get("time") or "03:00")
    local = datetime.fromtimestamp(now, TZ)
    target = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


def save(patch, now=None):
    """保存后台表单；只有开关/频率/时刻真的变了才重排下次执行时间。

    只改题量或科目不该把计时清零，否则每次微调都会让自动出题一直往后拖。
    """
    cfg = load()
    clean = normalize(patch, cfg)
    clean["last_run"] = cfg["last_run"]
    clean["runs"] = cfg["runs"]
    now = float(now if now is not None else time.time())
    timing_changed = any(clean[k] != cfg[k] for k in
                         ("enabled", "frequency", "time", "interval_hours"))
    if not clean["enabled"]:
        clean["next_run"] = 0.0
    elif timing_changed or not cfg["next_run"]:
        clean["next_run"] = next_run_at(clean, now)
    else:
        clean["next_run"] = cfg["next_run"]
    _save_cfg(clean)
    return clean


def clear_runs():
    cfg = load()
    cfg["runs"] = []
    _save_cfg(cfg)
    return cfg


def due(cfg, now=None):
    now = float(now if now is not None else time.time())
    return bool(cfg.get("enabled") and cfg.get("next_run") and
                now >= cfg["next_run"])


def status(st, now=None):
    """后台状态卡：下次/上次执行、题库里的自动题数、是否已配 Key。"""
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
    return {
        "server_now": now,
        "now_text": fmt_ts(now),
        "running": _run_lock.locked(),
        "next_run": cfg["next_run"],
        "next_run_text": fmt_ts(cfg["next_run"]) if cfg["enabled"] else "未启用",
        "next_run_relative": relative(cfg["next_run"], now) if cfg["enabled"] else "—",
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


def run_once(st, reason="schedule", now=None):
    """生成一批新题并入库；无论成功失败都推进下次执行时间并落一条记录。"""
    if not _run_lock.acquire(blocking=False):
        raise AutoBankError("上一次自动出题还在执行，请稍候再试")
    now = float(now if now is not None else time.time())
    t0 = time.time()
    rec = {
        "at": now, "at_text": fmt_ts(now), "reason": reason,
        "ok": False, "generated": 0, "added": 0, "skipped": 0,
        "total": 0, "pruned": 0, "source": "", "model": "",
        "bank_name": "", "latency": 0, "detail": "",
    }
    cfg = load()
    try:
        bank_id = cfg["bank_id"] or (st.first_bank() or {}).get("id", "")
        bank = st.bank(bank_id) if bank_id else None
        if not bank:
            raise AutoBankError("目标题库不存在，请在设置里重新选择一个题库")
        rec["bank_name"] = bank["name"]

        spec = {
            "stage": cfg["stage"], "grade": cfg["grade"],
            "subject": cfg["subject"], "count": cfg["count"],
            "topics": cfg["topics"], "mode": cfg["mode"],
            "avoid": [q["text"] for q in bank["questions"]][:600],
        }
        if cfg["mix"]:
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
        rec["ok"] = True
        rec["detail"] = result.get("note", "") or "已完成"
    except Exception as exc:  # 出题失败不能影响答题/放行主流程
        rec["detail"] = str(exc)
    finally:
        rec["latency"] = round(time.time() - t0, 1)
        try:
            latest = load()
            latest["last_run"] = now
            latest["next_run"] = next_run_at(latest, now) if latest["enabled"] else 0.0
            latest["runs"] = ([rec] + latest["runs"])[:MAX_RUNS]
            _save_cfg(latest)
        except Exception:
            pass
        _run_lock.release()
    return rec
