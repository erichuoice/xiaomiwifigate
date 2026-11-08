#!/usr/bin/env python3
"""运行时配置、题库分组与后台鉴权。

三个概念：
  题库（bank）  一组题目 + 一组奖励档位 + 一组受控设备 MAC
  档位（tier）  答对 N 道题 → 换 M 分钟上网时间（M = 0 表示一直放行）
  设备归属      每台受控设备只属于一个题库，不同题库管不同设备

两层配置：
  静态 config.json          端口、路由器凭证、后台密码、限流（容器里只读）
  可变 state/runtime.json   题库 / 档位 / 设备归属 / 提示语（可写、持久化）

题目字段：
  id / stage(小学|初中) / grade(年级) / subject(科目) / type(single|multi|judge)
  text / options[] / answer(int 或 int[]) / explain / star(1-5)

内置题库在 question_bank.json；首次启动按学段拆成「小学题库」「初中题库」。
"""

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WIFIGATE_CONFIG") or os.path.join(BASE, "config.json")
BUILTIN_PATH = os.path.join(BASE, "question_bank.json")
STATE = os.path.join(BASE, "state")
RUNTIME_PATH = os.path.join(STATE, "runtime.json")

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
BANK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")
SESSION_TTL = 8 * 3600
LOGIN_LIMIT = 5
LOGIN_WINDOW = 300
STAGES = ("小学", "初中", "高中")
MAX_OPTIONS = 8
MAX_MINUTES = 1440
MAX_TIER_QUESTIONS = 50
DEFAULT_TIERS = [{"questions": 3, "minutes": 30}, {"questions": 5, "minutes": 60}]


class StoreError(Exception):
    pass


# ---------------------------------------------------------------- 题目校验

def _norm_type(value):
    v = str(value or "").strip().lower()
    alias = {
        "single": "single", "单选": "single", "单选题": "single", "1": "single",
        "multi": "multi", "多选": "multi", "多选题": "multi", "2": "multi",
        "judge": "judge", "判断": "judge", "判断题": "judge", "3": "judge",
        "tf": "judge", "truefalse": "judge",
    }
    return alias.get(v, "single")


norm_type = _norm_type  # 供 importer 使用


def _norm_answer(value, n_options, qtype):
    """把各种答案写法归一成 int（单选/判断）或升序 int 列表（多选）。"""
    if qtype == "multi":
        raw = value
        if isinstance(raw, str):
            raw = re.split(r"[,\s、/;；|]+", raw.strip())
        elif isinstance(raw, int):
            raw = [raw]
        if not isinstance(raw, (list, tuple)) or not raw:
            raise StoreError("多选题缺少答案")
        out = []
        for item in raw:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                raise StoreError("多选题答案必须是选项序号")
            if not 0 <= idx < n_options:
                raise StoreError("多选题答案超出选项范围")
            if idx not in out:
                out.append(idx)
        if len(out) < 2:
            raise StoreError("多选题至少要选 2 个正确答案")
        return sorted(out)
    try:
        idx = int(value)
    except (TypeError, ValueError):
        raise StoreError("未设置正确答案")
    if not 0 <= idx < n_options:
        raise StoreError("正确答案超出选项范围")
    return idx


def _mk_id(text, used):
    base = "q" + hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    qid, n = base, 1
    while qid in used:
        n += 1
        qid = "%s-%d" % (base, n)
    return qid


def validate_question(q, index=1, used=None):
    if not isinstance(q, dict):
        raise StoreError("第 %s 题格式不正确" % index)
    text = str(q.get("text") or q.get("题干") or "").strip()
    if not text:
        raise StoreError("第 %s 题的题干不能为空" % index)
    qtype = _norm_type(q.get("type") or q.get("题型"))
    opts = q.get("options")
    if opts is None:
        opts = q.get("选项") or []
    if not isinstance(opts, list):
        raise StoreError("第 %s 题的选项格式不正确" % index)
    opts = [str(o).strip() for o in opts if str(o).strip()]
    if qtype == "judge" and not opts:
        opts = ["正确", "错误"]
    if not 2 <= len(opts) <= MAX_OPTIONS:
        raise StoreError("第 %s 题需要 2-%s 个选项" % (index, MAX_OPTIONS))
    if len(set(opts)) != len(opts):
        raise StoreError("第 %s 题有重复选项" % index)
    answer = _norm_answer(q.get("answer", q.get("答案")), len(opts), qtype)

    used = used if used is not None else set()
    qid = str(q.get("id") or "").strip()
    if not qid:
        qid = _mk_id(text, used)
    used.add(qid)

    stage = str(q.get("stage") or q.get("学段") or "").strip()
    if stage not in STAGES:
        stage = ""
    try:
        star = int(q.get("star", 2))
    except (TypeError, ValueError):
        star = 2
    star = min(5, max(1, star))
    return {
        "id": qid,
        "stage": stage,
        "grade": str(q.get("grade") or q.get("年级") or "").strip()[:16],
        "subject": str(q.get("subject") or q.get("科目") or "").strip()[:16],
        "type": qtype,
        "text": text[:400],
        "options": opts,
        "answer": answer,
        "explain": str(q.get("explain") or q.get("解析") or "").strip()[:400],
        "star": star,
    }


def validate_questions(raw, allow_empty=False):
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise StoreError("题库格式不正确")
    if not raw and not allow_empty:
        raise StoreError("题库至少要有一道题")
    clean, seen = [], set()
    for i, q in enumerate(raw, 1):
        item = validate_question(q, i, seen)
        if item["id"] in {c["id"] for c in clean}:
            item["id"] = _mk_id(item["text"], seen)
            seen.add(item["id"])
        clean.append(item)
    return clean


# ---------------------------------------------------------------- 档位校验

def validate_tiers(raw, total_questions, fallback_minutes=60, strict=True):
    """奖励档位：[{questions: 3, minutes: 30}, ...]，按题数升序去重。"""
    if not raw:
        raw = [{"questions": min(3, max(1, total_questions or 3)),
                "minutes": int(fallback_minutes)}]
    if not isinstance(raw, list):
        raise StoreError("奖励档位格式不正确")
    if len(raw) > 12:
        raise StoreError("奖励档位最多 12 档")
    out, seen = [], set()
    for i, t in enumerate(raw, 1):
        if isinstance(t, dict):
            qn, mins = t.get("questions"), t.get("minutes")
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            qn, mins = t
        else:
            raise StoreError("第 %s 档格式不正确" % i)
        try:
            qn, mins = int(qn), int(mins)
        except (TypeError, ValueError):
            raise StoreError("第 %s 档的题数和分钟数必须是整数" % i)
        if qn < 1:
            raise StoreError("第 %s 档的题数至少 1 道" % i)
        if qn > MAX_TIER_QUESTIONS:
            raise StoreError("第 %s 档的题数不能超过 %s 道" % (i, MAX_TIER_QUESTIONS))
        if total_questions and qn > total_questions:
            if strict:
                raise StoreError("第 %s 档要求 %s 道题，但本题库只有 %s 道"
                                 % (i, qn, total_questions))
            qn = total_questions
        if not 0 <= mins <= MAX_MINUTES:
            raise StoreError("第 %s 档的分钟数需在 0-%s 之间（0 = 一直放行）"
                             % (i, MAX_MINUTES))
        if qn in seen:
            if strict:
                raise StoreError("有重复的档位：%s 道题" % qn)
            continue
        seen.add(qn)
        out.append({"questions": qn, "minutes": mins})
    if not out:
        raise StoreError("至少设置一档奖励")
    out.sort(key=lambda t: t["questions"])
    return out


def validate_settings(settings, current):
    if not isinstance(settings, dict):
        raise StoreError("设置格式不正确")
    note = str(settings.get("note", current.get("note", "")))[:200]
    try:
        cap = int(settings.get("daily_cap_minutes",
                               current.get("daily_cap_minutes", 0)))
    except (TypeError, ValueError):
        raise StoreError("每日累计上限必须是数字")
    if not 0 <= cap <= MAX_MINUTES:
        raise StoreError("每日累计上限需在 0-%s 分钟之间（0 = 不限）" % MAX_MINUTES)
    return {"note": note, "daily_cap_minutes": cap}


def validate_macs(macs):
    if not isinstance(macs, list):
        raise StoreError("受控设备清单格式不正确")
    out = []
    for item in macs:
        mac = str(item).strip().lower().replace("-", ":")
        if not MAC_RE.match(mac):
            raise StoreError("MAC 地址格式不正确：%s" % item)
        if mac not in out:
            out.append(mac)
    return out


def validate_bank(data, strict=True):
    if not isinstance(data, dict):
        raise StoreError("题库格式不正确")
    name = str(data.get("name") or "").strip()[:24]
    if not name:
        raise StoreError("题库名称不能为空")
    questions = validate_questions(data.get("questions") or [], allow_empty=False)
    tiers = validate_tiers(data.get("tiers"), len(questions), strict=strict)
    devices = validate_macs(data.get("devices") or [])
    bid = str(data.get("id") or "").strip() or ("bank_" + secrets.token_hex(3))
    if not BANK_ID_RE.match(bid):
        raise StoreError("题库 ID 不合法（只能是字母、数字、下划线、短横线）")
    return {
        "id": bid,
        "name": name,
        "enabled": bool(data.get("enabled", True)),
        "tiers": tiers,
        "devices": devices,
        "questions": questions,
    }


def load_builtin_questions():
    with open(BUILTIN_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return validate_questions(data["questions"], allow_empty=True)


DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "max_wrong_per_window": 3,
        "lock_seconds": 60,
        "reblock_minutes": 60,
        "trust_x_forwarded_for": False,
        "admin_password": "",
        "note": "答对后可以上网，到时间会自动断开，需要再答一次。",
        "daily_cap_minutes": 0,
    },
    "router": {
        "mode": "mock",
        "model": "Xiaomi 路由器 BE7000 (RC06)",
        "base_url": "http://192.168.31.1",
        "admin_password": "",
        "stok": "",
        "login_path": "/cgi-bin/luci/api/xqsystem/login",
        "device_list_path": "/cgi-bin/luci/;stok={stok}/api/misystem/devicelist",
        "device_list_fallback_path": "/cgi-bin/luci/;stok={stok}/api/xqsystem/get_device_list",
        "set_mac_filter_path": "/cgi-bin/luci/;stok={stok}/api/xqsystem/set_mac_filter",
        "set_filter_method": "both",
        "wan_allow": "1",
        "wan_block": "0",
        "dry_run": True,
    },
}


def gen_password(groups=3, size=4):
    """生成好念好抄的密码，形如 oNfH-lLwi-3XN9。已排除易混字符 0/O/1/l/I。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(groups * size))
    return "-".join(raw[i:i + size] for i in range(0, len(raw), size))


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._config_file = CONFIG_PATH
        self._config_exists = os.path.isfile(CONFIG_PATH)
        if self._config_exists:
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    self._static = json.load(f)
            except (OSError, ValueError) as exc:
                raise StoreError("config.json 无法读取：%s" % exc)
        else:
            # 首次启动、还没配置：先用内置默认值把服务跑起来，
            # 所有页面跳转到 /setup，等用户填完路由器密码再落盘。
            self._static = copy.deepcopy(DEFAULT_CONFIG)
        self.trust_x_forwarded_for = bool(
            self._static["server"].get("trust_x_forwarded_for", False))

        os.makedirs(STATE, exist_ok=True)
        self._builtin = load_builtin_questions()
        self._runtime = self._load_runtime()
        self._sessions = {}
        self._login_fail = {}

    # ---- 首次配置 ----

    @property
    def configured(self):
        """是否已完成首次配置。

        判定标准是「路由器管理密码非空」，而不是「config.json 是否存在」：
        容器部署时入口脚本会先放一份空密码模板，文件存在但等于没配置。
        """
        with self._lock:
            router = self._static.get("router") or {}
            return bool(str(router.get("admin_password") or "").strip())

    @property
    def config_path(self):
        return self._config_file

    def save_config(self, router_password, admin_password=None,
                    base_url=None, mode="miwifi", dry_run=True):
        """首次配置：校验后写入 config.json（权限 600），立即生效，无需重启。

        仅在尚未配置时调用；已配置时由上层拒绝，避免未登录者改写密码。
        返回实际使用后台密码（用户留空时是自动生成的）。
        """
        if self.configured:
            raise StoreError("已经配置过了，如需重配请先删除 config.json 再重启")
        router_password = str(router_password or "").strip()
        if not router_password:
            raise StoreError("请填写路由器管理密码")
        mode = str(mode or "miwifi").strip().lower()
        if mode not in ("miwifi", "mock"):
            raise StoreError("路由器模式只能是 miwifi 或 mock")
        base_url = str(base_url or "").strip() or DEFAULT_CONFIG["router"]["base_url"]
        if not base_url.startswith(("http://", "https://")):
            raise StoreError("路由器地址要以 http:// 开头，例如 http://192.168.31.1")
        admin_password = str(admin_password or "").strip() or gen_password()
        if len(admin_password) < 6:
            raise StoreError("后台密码至少 6 位")

        with self._lock:
            cfg = copy.deepcopy(self._static)
            cfg.setdefault("server", {})
            cfg.setdefault("router", {})
            cfg["server"]["admin_password"] = admin_password
            cfg["router"].update({
                "mode": mode,
                "base_url": base_url.rstrip("/"),
                "admin_password": router_password,
                "stok": "",
                "dry_run": bool(dry_run),
            })
            tmp = self._config_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._config_file)
            self._static = cfg
            self._config_exists = True
            self.trust_x_forwarded_for = bool(
                cfg["server"].get("trust_x_forwarded_for", False))
        return {"admin_password": admin_password, "config_path": self._config_file,
                "mode": mode, "base_url": cfg["router"]["base_url"]}

    # ---- 初始化 / 迁移 ----

    def _fallback_minutes(self):
        return int(self._static["server"].get("reblock_minutes", 60) or 0)

    def _single_default_bank(self):
        static_quiz = self._static.get("quiz") or {}
        raw = static_quiz.get("questions") or []
        questions = (validate_questions(raw, allow_empty=True) if raw
                     else copy.deepcopy(self._builtin))
        if not questions:
            questions = copy.deepcopy(self._builtin)
        try:
            pick = int(static_quiz.get("pick", 3))
        except (TypeError, ValueError):
            pick = 3
        pick = min(max(1, pick), len(questions))
        return {
            "id": "bank_default", "name": "默认题库", "enabled": True,
            "devices": [],
            "tiers": [{"questions": pick, "minutes": self._fallback_minutes()}],
            "questions": questions,
        }

    def _default_banks(self):
        """首次启动：按学段把内置题库拆成两套，直观体现「不同题库管不同设备」。"""
        static_quiz = self._static.get("quiz") or {}
        if static_quiz.get("questions"):
            return [self._single_default_bank()]
        groups = {}
        for q in copy.deepcopy(self._builtin):
            groups.setdefault(q["stage"] or "未分学段", []).append(q)
        if len(groups) < 2:
            return [self._single_default_bank()]
        slug = {"小学": "primary", "初中": "junior", "高中": "senior"}
        banks = []
        for stage in ("小学", "初中", "高中"):
            if stage not in groups:
                continue
            banks.append({
                "id": "bank_%s" % slug.get(stage, secrets.token_hex(2)),
                "name": "%s题库" % stage, "enabled": True, "devices": [],
                "tiers": copy.deepcopy(DEFAULT_TIERS),
                "questions": groups.pop(stage),
            })
        for stage, qs in groups.items():
            banks.append({
                "id": "bank_" + secrets.token_hex(2), "name": "%s题库" % stage,
                "enabled": True, "devices": [],
                "tiers": copy.deepcopy(DEFAULT_TIERS), "questions": qs,
            })
        return banks

    def _default_runtime(self):
        s = self._static["server"]
        return {
            "version": 3,
            "banks": self._default_banks(),
            "settings": {
                "note": str(s.get("note", "")),
                "daily_cap_minutes": int(s.get("daily_cap_minutes", 0) or 0),
            },
        }

    def _dedupe_devices(self, banks):
        """一台设备只能归一个题库：先出现的题库优先。"""
        taken = set()
        for b in banks:
            keep = []
            for mac in b["devices"]:
                if mac in taken:
                    continue
                taken.add(mac)
                keep.append(mac)
            b["devices"] = keep

    def _normalize_runtime(self, data):
        base = self._default_runtime()
        raw_banks = data.get("banks")
        if isinstance(raw_banks, list) and raw_banks:
            banks = [validate_bank(b, strict=False) for b in raw_banks]
            seen = set()
            for b in banks:
                if b["id"] in seen:
                    b["id"] = "bank_" + secrets.token_hex(3)
                seen.add(b["id"])
        else:
            # v2 → v3 迁移：单题库 quiz + managed_devices
            quiz = data.get("quiz") or {}
            questions = validate_questions(quiz.get("questions") or [],
                                          allow_empty=True)
            if not questions:
                questions = copy.deepcopy(self._builtin)
            try:
                pick = min(max(1, int(quiz.get("pick", 3))), len(questions))
            except (TypeError, ValueError):
                pick = min(3, len(questions))
            old = data.get("settings") or {}
            minutes = int(old.get("reblock_minutes", self._fallback_minutes()))
            banks = [{
                "id": "bank_default", "name": "默认题库", "enabled": True,
                "devices": validate_macs(data.get("managed_devices") or []),
                "tiers": [{"questions": pick, "minutes": minutes}],
                "questions": questions,
            }]
        self._dedupe_devices(banks)
        settings = validate_settings(data.get("settings") or {}, base["settings"])
        return {"version": 3, "banks": banks, "settings": settings}

    def _load_runtime(self):
        if os.path.exists(RUNTIME_PATH):
            try:
                with open(RUNTIME_PATH, encoding="utf-8") as f:
                    return self._normalize_runtime(json.load(f))
            except (OSError, ValueError, StoreError):
                pass  # 文件损坏时按静态配置重建，避免服务起不来
        default = self._default_runtime()
        self._write(default)
        return default

    def _write(self, data):
        tmp = RUNTIME_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, RUNTIME_PATH)

    # ---- 读取 ----

    def snapshot(self):
        """合并静态与运行时配置；后台保存后立即生效，无需重启。"""
        with self._lock:
            cfg = copy.deepcopy(self._static)
            runtime = copy.deepcopy(self._runtime)
        cfg["banks"] = runtime["banks"]
        cfg["settings"] = runtime["settings"]
        cfg["server"]["note"] = runtime["settings"]["note"]
        cfg["server"]["daily_cap_minutes"] = runtime["settings"]["daily_cap_minutes"]
        cfg.pop("quiz", None)
        return cfg

    def banks(self):
        with self._lock:
            return copy.deepcopy(self._runtime["banks"])

    def bank(self, bank_id):
        with self._lock:
            for b in self._runtime["banks"]:
                if b["id"] == bank_id:
                    return copy.deepcopy(b)
        return None

    def first_bank(self):
        with self._lock:
            return copy.deepcopy(self._runtime["banks"][0])

    def bank_for_mac(self, mac):
        """按设备 MAC 找到它对应的题库；未归属任何题库则返回 None（不受控）。"""
        mac = str(mac or "").strip().lower().replace("-", ":")
        if not mac:
            return None
        with self._lock:
            for b in self._runtime["banks"]:
                if b.get("enabled") and mac in b["devices"]:
                    return copy.deepcopy(b)
        return None

    def summary(self):
        with self._lock:
            banks = self._runtime["banks"]
            macs = {m for b in banks for m in b["devices"]}
            return {
                "banks": len(banks),
                "questions": sum(len(b["questions"]) for b in banks),
                "managed_devices": len(macs),
                "by_bank": [{"id": b["id"], "name": b["name"],
                             "questions": len(b["questions"]),
                             "devices": len(b["devices"]),
                             "tiers": len(b["tiers"]),
                             "enabled": b["enabled"]} for b in banks],
            }

    # ---- 保存：题库 ----

    def save_bank(self, data):
        if not isinstance(data, dict):
            raise StoreError("题库格式不正确")
        with self._lock:
            bid = str(data.get("id") or "").strip()
            existing = next((b for b in self._runtime["banks"]
                             if b["id"] == bid), None) if bid else None
            clean = validate_bank(data)
            if existing:
                clean["id"] = existing["id"]
            elif any(b["id"] == clean["id"] for b in self._runtime["banks"]):
                raise StoreError("题库 ID 已存在")
            for b in self._runtime["banks"]:
                if b["id"] != clean["id"]:
                    b["devices"] = [m for m in b["devices"]
                                    if m not in clean["devices"]]
            if existing:
                self._runtime["banks"] = [clean if b["id"] == clean["id"] else b
                                          for b in self._runtime["banks"]]
            else:
                self._runtime["banks"].append(clean)
            self._write(self._runtime)
        return clean

    def delete_bank(self, bank_id):
        with self._lock:
            if len(self._runtime["banks"]) <= 1:
                raise StoreError("至少要保留一个题库")
            target = next((b for b in self._runtime["banks"]
                           if b["id"] == bank_id), None)
            if not target:
                raise StoreError("题库不存在")
            self._runtime["banks"] = [b for b in self._runtime["banks"]
                                      if b["id"] != bank_id]
            self._write(self._runtime)
        return {"removed": target["name"], "devices_released": len(target["devices"]),
                "banks": len(self._runtime["banks"])}

    def assign_device(self, mac, bank_id):
        """把一台设备分配给某个题库；bank_id 为空表示设为不受控。"""
        mac = validate_macs([mac])[0]
        bank_id = str(bank_id or "").strip()
        with self._lock:
            target = None
            for b in self._runtime["banks"]:
                b["devices"] = [m for m in b["devices"] if m != mac]
                if b["id"] == bank_id:
                    target = b
            if bank_id and not target:
                raise StoreError("题库不存在")
            if target:
                target["devices"].append(mac)
            self._write(self._runtime)
        return {"mac": mac, "bank_id": bank_id,
                "bank_name": target["name"] if target else "",
                "banks": copy.deepcopy(self._runtime["banks"])}

    def save_settings(self, settings):
        with self._lock:
            clean = validate_settings(settings, self._runtime["settings"])
            self._runtime["settings"] = clean
            self._write(self._runtime)
        return clean

    # ---- 导入 / 导出 ----

    def apply_questions(self, bank_id, questions, mode="append"):
        """把一批题目并入指定题库：append 按题干去重追加 / replace 覆盖。"""
        clean = validate_questions(questions)
        with self._lock:
            target = next((b for b in self._runtime["banks"]
                           if b["id"] == bank_id), None)
            if not target:
                raise StoreError("题库不存在")
            if mode == "replace":
                merged, added = clean, len(clean)
            else:
                current = target["questions"]
                seen_text = {q["text"] for q in current}
                seen_id = {q["id"] for q in current}
                merged, added = list(current), 0
                for q in clean:
                    if q["text"] in seen_text:
                        continue
                    if q["id"] in seen_id:
                        q["id"] = _mk_id(q["text"], seen_id)
                    seen_text.add(q["text"])
                    seen_id.add(q["id"])
                    merged.append(q)
                    added += 1
            # 题库变小后，超出范围的档位要收掉，否则永远答不到那个题数
            tiers = [t for t in target["tiers"] if t["questions"] <= len(merged)]
            if not tiers:
                tiers = [{"questions": min(3, len(merged)),
                          "minutes": target["tiers"][0]["minutes"]}]
            target["questions"] = merged
            target["tiers"] = tiers
            self._write(self._runtime)
        return {"added": added, "total": len(merged),
                "skipped": len(clean) - added, "tiers": tiers}

    def restore_builtin(self, bank_id, mode="replace"):
        """把内置题库灌进指定题库；题库名含「小学/初中」时只灌该学段。"""
        target = self.bank(bank_id)
        if not target:
            raise StoreError("题库不存在")
        questions = copy.deepcopy(self._builtin)
        for stage in ("小学", "初中", "高中"):
            if stage in target["name"]:
                picked = [q for q in questions if q["stage"] == stage]
                if picked:
                    questions = picked
                break
        return self.apply_questions(bank_id, questions, mode=mode)

    def export_all(self):
        with self._lock:
            data = copy.deepcopy(self._runtime)
        data["exported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["source"] = "wifi-gate"
        data["version"] = 3
        return data

    def import_all(self, data):
        """导入整份备份：题库 + 设置。旧版（单题库）备份也能读。"""
        if not isinstance(data, dict):
            raise StoreError("备份文件格式不正确")
        with self._lock:
            current = self._runtime
            normalized = self._normalize_runtime(data)
            if not data.get("banks") and not data.get("quiz"):
                normalized["banks"] = current["banks"]
            if not data.get("settings") and not data.get("server"):
                normalized["settings"] = current["settings"]
            self._runtime = normalized
            self._write(self._runtime)
        return {"banks": len(normalized["banks"]),
                "questions": sum(len(b["questions"]) for b in normalized["banks"]),
                "managed_devices": len({m for b in normalized["banks"]
                                        for m in b["devices"]})}

    # ---- 后台鉴权 ----

    def admin_password(self):
        return str(self._static["server"].get("admin_password") or "")

    def login_wait(self, ip):
        with self._lock:
            rec = self._login_fail.get(ip)
            if rec and rec[0] >= LOGIN_LIMIT:
                left = LOGIN_WINDOW - (time.time() - rec[1])
                if left > 0:
                    return int(left)
                self._login_fail.pop(ip, None)
        return 0

    def login(self, password, ip):
        wait = self.login_wait(ip)
        if wait:
            raise StoreError("密码错误次数过多，请 %s 秒后再试" % wait)
        expected = self.admin_password()
        if not expected:
            raise StoreError("未设置 server.admin_password，后台已禁用")
        if not hmac.compare_digest(str(password or ""), expected):
            with self._lock:
                rec = self._login_fail.get(ip) or [0, time.time()]
                rec[0] += 1
                self._login_fail[ip] = rec
            raise StoreError("管理密码不正确")
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[token] = time.time() + SESSION_TTL
            self._login_fail.pop(ip, None)
        return token

    def check_session(self, token):
        if not token:
            return False
        with self._lock:
            expire = self._sessions.get(token)
            if not expire:
                return False
            if expire < time.time():
                self._sessions.pop(token, None)
                return False
            return True

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)
