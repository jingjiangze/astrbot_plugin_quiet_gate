"""表情暂存（Emoji Hold）—— 把表情当情绪信号，而不是信息。

只发表情时不回复，但也不丢弃：先把表情和它表达的情绪**暂存**起来；
同一用户在 TTL 内发来文字时，把情绪作为背景注入这次回复；超时自动失效。

设计取舍：
  * 只拦本次 LLM 请求（on_llm_request + stop_event），不中断事件流，
    所以表情包采集、图搜等其它插件照常工作。
  * 情绪识别只用确定性映射：unicode emoji 可靠映射；QQ 表情按 id 查表，
    认不出来就标 unknown_label，不臆测。
  * 画像按用户累计计数，样本不足不注入，避免用两三个表情就下结论。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Face, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # AstrBot >= 4.29 官方数据目录接口
    from astrbot.api.star import StarTools
except Exception:  # pragma: no cover - 兼容旧版
    try:
        from astrbot.core.star.star_tools import StarTools
    except Exception:
        StarTools = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_quiet_gate"
DEFAULT_DATA_DIR = Path("/opt/astrbot/data/plugin_data") / PLUGIN_NAME

DEFAULTS = {
    "enable": True,
    "ttl_seconds": 120,
    "profile_min_samples": 5,
    "max_pending_per_user": 4,
    "private_enable": True,
    "group_whitelist": [],
    "group_blacklist": [],
    "unknown_label": "情绪不明",
    "show_hint_to_user": False,
}

# 去掉非文字成分：空白 / 标点 / 全角 / emoji / [表情] 这类占位符
_BRACKET = re.compile(r"\[[^\]\n]{1,12}\]")
_NOISE = re.compile(
    r"[\s\u3000\u2000-\u206f\u2e00-\u2e7f\u3001-\u303f\uff00-\uffef"
    r"\U0001F000-\U0001FAFF\u2600-\u27bf\ufe00-\ufe0f\u2190-\u21ff"
    r"\.\,\!\?\~\(\)\[\]\{\}\<\>\-\+\=\*\/\\\|]+"
)

# unicode emoji -> 情绪（常见可靠映射）
UNICODE_EMOTION = {
    "😂": "大笑", "🤣": "大笑", "😄": "开心", "😀": "开心", "😊": "开心", "🙂": "平静",
    "😭": "难过", "😢": "难过", "🥲": "难过", "😞": "失落", "😔": "失落",
    "😡": "生气", "😠": "生气", "🤬": "暴怒",
    "😱": "震惊", "😨": "害怕", "😰": "紧张", "😳": "尴尬",
    "🥺": "求安慰", "🥹": "求安慰", "🥰": "喜爱", "😍": "喜爱", "❤": "喜爱", "💔": "失落",
    "😏": "调侃", "😅": "尴尬", "🙃": "无语", "😑": "无语", "😐": "无语", "🤔": "思考",
    "😴": "困", "🥱": "困", "🤮": "反感", "🤢": "反感", "👍": "认可", "👎": "否定",
    "🙏": "拜托", "🎉": "庆祝", "🔥": "强烈认同", "💀": "无语", "🤡": "自嘲",
}

# QQ 表情 id -> 情绪（best effort，可在 emotion_map.json 覆盖；未收录一律 unknown_label）
QQ_FACE_EMOTION = {
    14: "难过", 15: "无语", 16: "无语", 18: "无语", 19: "生气", 20: "得意",
    21: "害羞", 22: "害羞", 23: "生气", 24: "生气", 25: "难受",
    26: "震惊", 27: "尴尬", 28: "无语", 29: "无奈", 30: "开心",
    32: "疑问", 33: "疑问", 34: "求安慰", 41: "求安慰", 49: "开心",
    53: "开心", 60: "无奈", 66: "开心", 74: "无语", 75: "无语",
    76: "无语", 77: "无语", 78: "无语", 79: "无语", 100: "难过",
    101: "无语", 102: "无语", 103: "开心", 104: "开心", 105: "调侃",
    106: "调侃", 107: "无奈", 108: "无奈", 109: "求安慰", 110: "难过",
    111: "难过", 112: "无语", 116: "无语", 118: "大笑", 119: "困", 120: "无语",
    178: "大笑", 179: "失落", 180: "生气", 181: "生气", 182: "无语",
    183: "生气", 185: "震惊", 187: "无语", 201: "无语", 212: "无语",
    214: "无语", 219: "无语", 264: "认可", 265: "认可", 271: "认可",
    272: "认可", 273: "认可", 274: "认可",
}


def meaningful_text(chain) -> str:
    """返回消息里「有意义的文字」（去掉标点、emoji、[表情] 占位符后）。"""
    parts = []
    for seg in chain:
        if isinstance(seg, Plain):
            parts.append(getattr(seg, "text", "") or "")
    raw = _BRACKET.sub("", "".join(parts))
    return _NOISE.sub("", raw).strip()


def collect_emotions(chain, face_map, unicode_map, unknown="情绪不明") -> list[str]:
    """从消息链里抽取情绪标签（按出现顺序去重）。"""
    out: list[str] = []
    for seg in chain:
        if isinstance(seg, Face):
            fid = int(getattr(seg, "id", -1) or -1)
            label = face_map.get(fid) or face_map.get(str(fid)) or unknown  # type: ignore[arg-type]
            out.append(label)
        elif isinstance(seg, Plain):
            for ch in getattr(seg, "text", "") or "":
                if ch in unicode_map:
                    out.append(unicode_map[ch])
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # pragma: no cover
        logger.warning("[quiet_gate] 写入 %s 失败: %r", path.name, exc)


class QuietGate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = dict(DEFAULTS)
        try:
            cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
        except Exception:
            pass
        self.cfg = cfg

        base = None
        try:
            if StarTools is not None:
                base = Path(StarTools.get_data_dir())
        except Exception:
            base = None
        self.data_dir = base or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.data_dir / "pending.json"
        self.profile_file = self.data_dir / "profile.json"
        self.map_file = self.data_dir / "emotion_map.json"
        self._lock = asyncio.Lock()

        if not self.map_file.exists():
            _write_json(self.map_file, {
                "qq_face": {str(k): v for k, v in QQ_FACE_EMOTION.items()},
                "unicode": UNICODE_EMOTION,
            })

        self.face_map: dict = dict(QQ_FACE_EMOTION)
        self.unicode_map: dict = dict(UNICODE_EMOTION)
        raw = _read_json(self.map_file, {})
        try:
            if isinstance(raw.get("qq_face"), dict):
                self.face_map.update({int(k): v for k, v in raw["qq_face"].items()})
            if isinstance(raw.get("unicode"), dict):
                self.unicode_map.update(raw["unicode"])
        except Exception as exc:
            logger.warning("[quiet_gate] emotion_map 解析失败，用内置表: %r", exc)

        logger.info(
            "[quiet_gate] 已加载 v%s | ttl=%ss profile_min=%s data=%s",
            "3.0.0", self.cfg["ttl_seconds"], self.cfg["profile_min_samples"], self.data_dir,
        )

    # ------------------------------------------------------------------ 工具
    @property
    def ttl(self) -> int:
        return max(10, int(self.cfg.get("ttl_seconds") or 120))

    @property
    def unknown(self) -> str:
        return str(self.cfg.get("unknown_label") or "情绪不明")

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        try:
            gid = str(event.get_group_id() or "")
            if not gid:                                  # 私聊
                return bool(self.cfg.get("private_enable", True))
            wl = {str(x) for x in (self.cfg.get("group_whitelist") or [])}
            bl = {str(x) for x in (self.cfg.get("group_blacklist") or [])}
            if gid in bl:
                return False
            if wl and gid not in wl:
                return False
            return True
        except Exception:
            return True

    def _key(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    def _load_pending(self) -> dict:
        now = time.time()
        data = _read_json(self.state_file, {}) or {}
        try:
            return {k: v for k, v in data.items()
                    if now - float(v.get("ts", 0)) < self.ttl and v.get("items")}
        except Exception:
            return {}

    def _bump_profile(self, sender: str, emotions: list[str]) -> None:
        prof = _read_json(self.profile_file, {}) or {}
        rec = prof.get(sender) or {"counts": {}, "total": 0}
        counts = rec.get("counts") or {}
        for e in emotions:
            counts[e] = int(counts.get(e, 0)) + 1
        rec["counts"] = counts
        rec["total"] = int(rec.get("total", 0)) + len(emotions)
        rec["updated"] = int(time.time())
        prof[sender] = rec
        _write_json(self.profile_file, prof)

    def _profile_hint(self, sender: str) -> str:
        prof = _read_json(self.profile_file, {}) or {}
        rec = prof.get(sender) or {}
        if int(rec.get("total", 0)) < int(self.cfg.get("profile_min_samples") or 5):
            return ""
        counts = rec.get("counts") or {}
        if not counts:
            return ""
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:2]
        desc = "、".join(f"{k}×{v}" for k, v in top)
        return f"（此人的表情历史以 {desc} 为主，可据此判断他的表达习惯）"

    # ------------------------------------------------------------ 事件：暂存
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def hold_emoji(self, event: AstrMessageEvent):
        if not self.cfg.get("enable", True) or not self._scope_ok(event):
            return
        try:
            chain = event.get_messages()
        except Exception:
            return
        if not chain:
            return

        self_id = str(event.get_self_id() or "")
        has_face = False
        for seg in chain:
            if isinstance(seg, At) and str(getattr(seg, "qq", "")) == self_id:
                return                                  # 明确 @ 了机器人
            if isinstance(seg, Plain) and meaningful_text([seg]):
                return                                  # 有文字，正常处理
            if isinstance(seg, Face):
                has_face = True

        emotions = collect_emotions(chain, self.face_map, self.unicode_map, self.unknown)
        if not emotions and not has_face:
            return                                      # 纯空白/标点，不当情绪

        key = self._key(event)
        async with self._lock:
            data = self._load_pending()
            item = data.get(key) or {"items": [], "ts": 0}
            items = list(item.get("items") or [])
            items.append({"emotions": emotions or [self.unknown], "at": int(time.time())})
            item["items"] = items[-max(1, int(self.cfg.get("max_pending_per_user") or 4)):]
            item["ts"] = int(time.time())
            data[key] = item
            _write_json(self.state_file, data)
            self._bump_profile(str(event.get_sender_id() or ""), emotions or [self.unknown])

        logger.debug("[quiet_gate] 暂存表情 %s (ttl=%ss)", emotions, self.ttl)
        event.stop_event()

    # -------------------------------------------------------- LLM：注入情绪
    @filter.on_llm_request(priority=1)
    async def inject_emotion(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.get("enable", True) or not self._scope_ok(event):
            return
        key = self._key(event)
        async with self._lock:
            data = self._load_pending()
            item = data.pop(key, None)
            if item:
                _write_json(self.state_file, data)
        if not item:
            return

        emotions: list[str] = []
        for it in item.get("items") or []:
            for e in it.get("emotions") or []:
                if e not in emotions:
                    emotions.append(e)
        if not emotions:
            return

        age = int(time.time() - float(item.get("ts", 0)))
        hint = (
            f"（对方刚才先发了表情、没有文字，情绪倾向：{'、'.join(emotions)}，距今 {age} 秒。"
            "表情通常只表达情绪、不是信息；请结合他这一句文字来理解，不要就表情本身追问。）"
            + self._profile_hint(str(event.get_sender_id() or ""))
        )
        try:
            req.system_prompt = (req.system_prompt or "") + "\n" + hint
            logger.debug("[quiet_gate] 注入情绪 %s (age=%ss)", emotions, age)
        except Exception as exc:
            logger.warning("[quiet_gate] 注入失败: %r", exc)

    # ------------------------------------------------------------------ 命令
    @filter.command("表情暂存")
    async def cmd_pending(self, event: AstrMessageEvent):
        data = self._load_pending()
        item = data.get(self._key(event))
        if not item:
            yield event.plain_result("当前没有暂存的表情。")
            return
        emo = []
        for it in item.get("items") or []:
            emo.extend(it.get("emotions") or [])
        age = int(time.time() - float(item.get("ts", 0)))
        yield event.plain_result(f"暂存 {len(item.get('items') or [])} 个表情，情绪：{'、'.join(emo)}（{age}s 前，{self.ttl}s 后失效）")
        event.stop_event()

    @filter.command("表情画像")
    async def cmd_profile(self, event: AstrMessageEvent):
        sid = str(event.get_sender_id() or "")
        prof = _read_json(self.profile_file, {}) or {}
        rec = prof.get(sid) or {}
        if not rec.get("total"):
            yield event.plain_result("还没有攒到表情样本。")
            event.stop_event()
            return
        counts = rec.get("counts") or {}
        top = "、".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:5])
        yield event.plain_result(f"表情样本 {rec.get('total')} 次：{top}")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("表情画像清除")
    async def cmd_profile_clear(self, event: AstrMessageEvent):
        _write_json(self.profile_file, {})
        yield event.plain_result("已清空全部表情画像。")
        event.stop_event()
