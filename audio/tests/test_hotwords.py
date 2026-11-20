import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import audio_caption  # noqa: F401  (puts ../gateway on sys.path when run from a checkout)
from audio_caption.hotwords import Hotwords


class HotwordTests(unittest.TestCase):
    def test_scene_priority_legacy_names_and_real_token_budget_interface(self):
        raw = ("Schmidt, Frau Maier\n[设备展]\n" + "A" * 400 + "=超长\nWerkzeugabnahme=模具验收\n"
               "[医学]\nMikroskop=显微镜\n[]\n" + "\n".join(f"Halter{i}=支架{i}" for i in range(500)))
        glossary = Hotwords(raw)
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


if __name__ == "__main__":
    unittest.main()
