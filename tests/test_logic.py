"""离线单测：不依赖 AstrBot 运行时（用桩替换 astrbot.* 模块）。

运行：
    python -m unittest discover -s tests -v
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _install_stubs(data_dir: Path) -> None:
    """构造最小 astrbot 桩，让 main.py 可被导入。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

    class _FilterNamespace:
        class EventMessageType:
            ALL = "ALL"

        class PermissionType:
            ADMIN = "ADMIN"

        @staticmethod
        def _identity(*a, **k):
            def deco(fn):
                return fn
            return deco

        event_message_type = _identity
        on_llm_request = _identity
        command = _identity
        permission_type = _identity

    event = types.ModuleType("astrbot.api.event")
    event.filter = _FilterNamespace()

    class AstrMessageEvent:  # noqa: D401
        pass

    event.AstrMessageEvent = AstrMessageEvent

    comps = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Face:
        def __init__(self, id=-1):
            self.id = id

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    comps.Plain, comps.Face, comps.At = Plain, Face, At

    prov = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        system_prompt = ""

    prov.ProviderRequest = ProviderRequest

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        _dir = str(data_dir)

        @classmethod
        def get_data_dir(cls):
            return cls._dir

    star.Context, star.Star, star.StarTools = Context, Star, StarTools

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": comps,
        "astrbot.api.provider": prov,
        "astrbot.api.star": star,
    })


class QuietGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="quiet_gate_")
        _install_stubs(Path(cls.tmp))
        spec = importlib.util.spec_from_file_location("quiet_gate_main", ROOT / "main.py")
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.gate = cls.m.QuietGate(None, {"ttl_seconds": 120, "profile_min_samples": 5})

    # ---------------- 文字判定 ----------------
    def test_meaningful_text(self):
        m, Plain = self.m, sys.modules["astrbot.api.message_components"].Plain
        self.assertEqual(m.meaningful_text([Plain(text="😂😂")]), "")
        self.assertEqual(m.meaningful_text([Plain(text="[表情]")]), "")
        self.assertEqual(m.meaningful_text([Plain(text="。。。")]), "")
        self.assertEqual(m.meaningful_text([Plain(text="  ")]), "")
        self.assertEqual(m.meaningful_text([Plain(text="你好")]), "你好")
        self.assertEqual(m.meaningful_text([Plain(text="😀哈哈")]), "哈哈")

    # ---------------- 情绪抽取 ----------------
    def test_collect_emotions(self):
        m = self.m
        comps = sys.modules["astrbot.api.message_components"]
        self.assertEqual(m.collect_emotions([comps.Face(id=14)], m.QQ_FACE_EMOTION, m.UNICODE_EMOTION), ["难过"])
        self.assertEqual(m.collect_emotions([comps.Face(id=99999)], m.QQ_FACE_EMOTION, m.UNICODE_EMOTION), ["情绪不明"])
        self.assertEqual(m.collect_emotions([comps.Plain(text="😭")], m.QQ_FACE_EMOTION, m.UNICODE_EMOTION), ["难过"])
        # 去重
        self.assertEqual(m.collect_emotions([comps.Plain(text="😂😂")], m.QQ_FACE_EMOTION, m.UNICODE_EMOTION), ["大笑"])

    # ---------------- TTL ----------------
    def test_ttl_expiry(self):
        gate = self.gate
        import time
        stale = {"k": {"items": [{"emotions": ["难过"], "at": 0}], "ts": time.time() - 10_000}}
        gate.state_file.write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(gate._load_pending(), {})

        fresh = {"k": {"items": [{"emotions": ["难过"], "at": 0}], "ts": time.time() - 5}}
        gate.state_file.write_text(json.dumps(fresh), encoding="utf-8")
        self.assertIn("k", gate._load_pending())

    # ---------------- 画像 ----------------
    def test_profile_threshold(self):
        gate = self.gate
        gate._bump_profile("u1", ["大笑", "大笑", "难过"])
        self.assertEqual(gate._profile_hint("u1"), "")
        gate._bump_profile("u1", ["大笑", "难过"])
        hint = gate._profile_hint("u1")
        self.assertIn("大笑×3", hint)
        self.assertIn("难过×2", hint)

    # ---------------- 作用域 ----------------
    def test_scope(self):
        gate = self.gate
        ev = types.SimpleNamespace(get_group_id=lambda: "", unified_msg_origin="x", get_sender_id=lambda: "1")
        self.assertTrue(gate._scope_ok(ev))                    # 私聊默认生效

        gate.cfg["private_enable"] = False
        self.assertFalse(gate._scope_ok(ev))
        gate.cfg["private_enable"] = True

        grp = types.SimpleNamespace(get_group_id=lambda: "123", unified_msg_origin="g", get_sender_id=lambda: "1")
        gate.cfg["group_whitelist"] = ["456"]
        self.assertFalse(gate._scope_ok(grp))
        gate.cfg["group_whitelist"] = []
        gate.cfg["group_blacklist"] = ["123"]
        self.assertFalse(gate._scope_ok(grp))
        gate.cfg["group_blacklist"] = []
        self.assertTrue(gate._scope_ok(grp))

    # ---------------- 配置 schema 自洽 ----------------
    def test_schema_matches_defaults(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in schema:
            self.assertIn(key, self.m.DEFAULTS, f"schema 键 {key} 在 DEFAULTS 中缺失")
        for key in self.m.DEFAULTS:
            self.assertIn(key, schema, f"DEFAULTS 键 {key} 在 schema 中缺失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
