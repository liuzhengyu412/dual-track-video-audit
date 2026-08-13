from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import audit_pipeline as pipeline
import models
from common import ACTIVE_AUDIT_CONFIG, AuditError
from reporting import excel_safe
from results import routing_review_items, sanitize_asr_sentences, sanitize_evidence


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        pipeline.DEFAULT_SETTINGS_PATH = PROJECT_DIR / "settings.json"
        pipeline.configure_audit_config(PROJECT_DIR / "rules" / "audit_config.json")

    def test_routing_result_survives_formal_main_without_reload(self) -> None:
        ACTIVE_AUDIT_CONFIG["rule_routing_result"] = {
            "visual_requirements": "只看画面",
            "audio_requirements": "只看转写",
            "combined_requirements": "两侧都要检查",
            "manual_or_system_requirements": "查询外部系统",
            "uncertain_items": ["待确认规则"],
        }
        self.assertIn("只看画面", pipeline.global_instruction("visual"))
        self.assertIn("只看转写", pipeline.global_instruction("audio"))
        self.assertIn("两侧都要检查", pipeline.global_instruction("visual"))

    def test_routing_manual_items_are_explicit(self) -> None:
        items = routing_review_items(
            {
                "manual_or_system_requirements": "需要查数据库",
                "uncertain_items": ["无法判断来源"],
            }
        )
        self.assertEqual(2, len(items))
        self.assertTrue(all(item["status"] == "人工复核" for item in items))

    def test_excel_formula_text_is_escaped(self) -> None:
        self.assertEqual("'=1+1", excel_safe("=1+1"))
        self.assertEqual("正常内容", excel_safe("正常内容"))

    def test_review_output_preserves_real_information(self) -> None:
        phone = "13800138000"
        evidence = sanitize_evidence(
            [{"start": "00:00:01", "end": "00:00:02", "detail": phone}],
            "音频转写",
        )
        transcript = sanitize_asr_sentences(
            [{"segment_id": "SEG-001", "start": "00:00:01", "end": "00:00:02", "text": phone}]
        )
        self.assertIn(phone, evidence[0]["detail"])
        self.assertEqual(phone, transcript[0]["text"])

    def test_custom_settings_and_advanced_rules_load_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            settings = {
                "mode": "advanced",
                "default_scenario": "review",
                "script_comparison": {"enabled": False},
                "speaker_roles": ["agent", "client", "unknown"],
            }
            config = {
                "rules": [
                    {
                        "id": "rule_a",
                        "category": "通用",
                        "name": "示例规则",
                        "method": "模型",
                        "tracks": ["audio"],
                    }
                ],
                "scenarios": {"review": {"name": "审核", "script": ""}},
                "prompts": {},
                "local_validators": {},
            }
            settings_path = temp / "settings.json"
            config_path = temp / "audit_config.json"
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            pipeline.DEFAULT_SETTINGS_PATH = settings_path
            loaded = pipeline.configure_audit_config(config_path)
            self.assertEqual("advanced", loaded["mode"])
            self.assertEqual(["agent", "client", "unknown"], loaded["speaker_roles"])
            self.assertEqual(["rule_a"], [rule.rule_id for rule in loaded["rules"]])

    def test_asr_timeout_retries_with_smaller_wav_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            wav_path = temp / "long.wav"
            with wave.open(str(wav_path), "wb") as target:
                target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                target.writeframes(b"\x00\x00" * 16000 * 31)
            chunk = {
                "index": 1,
                "path": wav_path,
                "offset_ms": 0,
                "duration_seconds": 31.0,
                "size_bytes": wav_path.stat().st_size,
            }
            calls = 0

            def fake_call(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise AuditError("timeout")
                return {
                    "request_id": "test",
                    "sentences": [
                        {"begin_ms": 0, "end_ms": 1000, "text": "测试", "words": []}
                    ],
                }

            with patch.object(models, "call_fun_asr_chunk", side_effect=fake_call):
                result = models.transcribe_audio_chunks(
                    chunks=[chunk], api_key="test", model="test", api_base="https://example.com/api/v1"
                )
            self.assertGreaterEqual(calls, 2)
            self.assertTrue(result["sentences"])


if __name__ == "__main__":
    unittest.main()
