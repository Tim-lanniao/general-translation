import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from teams_caption.gateway import translation_request
from teams_caption.glossary import Glossary


class GlossaryTests(unittest.TestCase):
    def test_exact_phonetic_and_product_boundaries(self):
        glossary = Glossary("Pass=通行证\nSiemens=西门子\nSiemens Energy=西门子能源\n"
                            "Blauwal=蓝鲸\nSchäfer=舍费尔\nGX-120=样机\nMeyer=迈尔\nMeier=梅尔")
        hints = glossary.translation_hints("Siemens Energy, Blauwahl, Schaefer und GX 120", "")
        self.assertEqual(hints.splitlines(), ["Siemens Energy=西门子能源", "Blauwal=蓝鲸", "Schäfer=舍费尔", "GX-120=样机"])
        self.assertEqual(glossary.translation_hints("Passwort GX-121 und Mayer", ""), "")
        self.assertEqual(glossary.translation_hints("GX120 oder GX-120B", ""), "GX-120=样机")
        self.assertEqual(glossary.translation_hints("Meier", ""), "Meier=梅尔")
        self.assertEqual(glossary.translation_hints("Das Herz des Blauwals", ""), "Blauwal=蓝鲸")
        self.assertEqual(Glossary("Maße=尺寸\nMasse=质量").translation_hints("Maße und Masse", ""), "Maße=尺寸\nMasse=质量")

    def test_scene_priority_legacy_names_and_real_token_budget_interface(self):
        raw = ("Schmidt, Frau Maier\n[设备展]\n" + "A" * 400 + "=超长\nWerkzeugabnahme=模具验收\n"
               "[医学]\nMikroskop=显微镜\n[]\n" + "\n".join(f"Halter{i}=支架{i}" for i in range(500)))
        glossary = Glossary(raw)
        tokenizer = Mock()
        tokenizer.encode.side_effect = lambda text, *, add_special_tokens: SimpleNamespace(ids=list(text.encode()))
        prompt = glossary.recognition_prompt("周五设备展", "Halter499", tokenizer)
        self.assertLessEqual(len((" " + prompt).encode()), 223)
        self.assertLessEqual(len(prompt.split(",")), 32)
        for name in ("Halter499", "Werkzeugabnahme", "Schmidt", "Frau Maier"):
            self.assertIn(name, prompt)
        for other in ("验收", "Mikroskop", "AAAA", "["):
            self.assertNotIn(other, prompt)
        self.assertIn("Halter499", glossary.recognition_prompt("支架499验收", "", tokenizer))
        self.assertEqual(glossary.translation_hints("Frau Maier bestellt Halter499.", ""), "Frau Maier\nHalter499=支架499")

    def test_conflicting_scene_translations_and_bounded_prompt(self):
        raw = "[物流]\nLager=仓库\n[轴承展]\nLager=轴承\n[]\n"
        glossary = Glossary(raw + "\n".join(f"Modul{i}=产品{i}" for i in range(80)))
        self.assertEqual(glossary.translation_hints("Das Lager", "轴承展"), "Lager=轴承")
        self.assertEqual(glossary.translation_hints("Das Lager", ""), "")
        hints = glossary.translation_hints(" ".join(f"Modul{i}" for i in range(80)), "")
        self.assertEqual(len(hints.splitlines()), 12)
        self.assertLessEqual(len(hints), 512)

    def test_phonetic_hotwords_only_help_audio_and_gateway_owns_target(self):
        source = "Der Blauwahl."
        local = Glossary("Blauwal=旧译")
        shared = Glossary("Blauwal=蓝鲸", phonetic=False)
        names = [t.source for t in local.matches(source, "", near_only=True)]
        self.assertEqual(names, ["Blauwal"])
        body = {"source": source, "asr_names": names}
        for path, expected in (("/v1/captions", True), ("/v1/translate", False)):
            prompt = translation_request(path, body, {"llm_model": "fake"}, shared)["messages"][0]["content"]
            self.assertEqual("Blauwal=蓝鲸" in prompt, expected)
            self.assertNotIn("旧译", prompt)

    def test_only_current_matches_reach_gateway_prompts(self):
        raw = "Presse=压力机\nGX-100=样机\n" + "\n".join(f"Katalog{i}=目录{i}" for i in range(500))
        glossary = Glossary(raw)
        messages = [{"role": "user", "content": "GX-100"}, {"role": "assistant", "content": "收到"},
                    {"role": "user", "content": "Die Presse"}]
        for path, body in (("/v1/captions", {"source": "Die Presse", "context": "GX-100"}),
                           ("/v1/translate", {"source": "Die Presse"}),
                           ("/v1/chat/completions", {"messages": messages, "chat_template_kwargs": {"enable_thinking": True}})):
            payload = translation_request(path, body, {"llm_model": "test"}, glossary)
            contents = json.dumps(payload["messages"], ensure_ascii=False)
            self.assertIn("Presse=压力机", contents)
            self.assertNotIn("样机", contents)
            self.assertNotIn("Katalog", contents)
            self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
