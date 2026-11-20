"""Data directory split, the shared-glossary endpoint and the gateway's independence from other components."""
import ast
import http.client
import ipaddress
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import isolated_data
from translation_gateway import access, client, config
from translation_gateway.server import Gateway, Handler, Server

PACKAGE = Path(config.__file__).resolve().parent


class LegacyLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_data(self).parent

    def test_single_package_data_root_is_split_once(self):
        legacy = {"source": "device:custom", "terms": "Messe", "topic": "周会", "font_px": 30, "cache_dir": "C:/cache",
                  "gpu_lock": "C:/gpu.lock", "llm_controller": "C:/strata.ps1", "llm_model": "qwen", "gateway_lan": True}
        (self.root / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        (self.root / "gateway-terms.txt").write_text("Messe=展会", encoding="utf-8")
        (self.root / "gateway-devices.json").write_text('{"devices": []}', encoding="utf-8")
        (self.root / "model-test.active").touch()
        (self.root / "会议记录" / "2026-11-19_100000").mkdir(parents=True)
        (self.root / "会议记录" / "2026-11-19_100000" / "captions.md").write_text("记录", encoding="utf-8")
        (self.root / "logs").mkdir()
        for name in ("teams-caption.log", "translation-gateway.log"):
            (self.root / "logs" / name).write_text(name, encoding="utf-8")

        self.assertTrue(config.migrate_legacy())
        gateway = json.loads((self.root / "gateway" / "config.json").read_text(encoding="utf-8"))
        audio = json.loads((self.root / "audio" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(gateway, {"cache_dir": "C:/cache", "gpu_lock": "C:/gpu.lock", "llm_controller": "C:/strata.ps1",
                                   "llm_model": "qwen", "gateway_lan": True})
        self.assertEqual(audio, {"source": "device:custom", "terms": "Messe", "topic": "周会", "font_px": 30, "cache_dir": "C:/cache"})
        self.assertEqual(config.read_terms(), "Messe=展会")
        self.assertTrue((self.root / "gateway" / "devices.json").exists())
        self.assertTrue((self.root / "gateway" / "model-test.active").exists())
        self.assertEqual((self.root / "audio" / "会议记录" / "2026-11-19_100000" / "captions.md").read_text(encoding="utf-8"), "记录")
        self.assertTrue((self.root / "audio" / "logs" / "teams-caption.log").exists())
        self.assertTrue((self.root / "gateway" / "logs" / "translation-gateway.log").exists())
        self.assertEqual(json.loads((self.root / "config.legacy.json").read_text(encoding="utf-8")), legacy)
        for gone in ("config.json", "gateway-terms.txt", "会议记录", "logs"):
            self.assertFalse((self.root / gone).exists(), gone)

        self.assertFalse(config.migrate_legacy())  # nothing left to move
        self.assertEqual(config.load()["llm_controller"], "C:/strata.ps1")

    def test_terms_from_before_the_shared_glossary_become_the_gateway_glossary(self):
        (self.root / "config.json").write_text(json.dumps({"terms": "Presse=压力机"}), encoding="utf-8")
        config.load()
        self.assertEqual(config.read_terms(), "Presse=压力机")

    def test_new_installations_need_no_migration(self):
        self.assertFalse(config.migrate_legacy())
        self.assertEqual(config.load()["gateway_port"], 18083)
        self.assertEqual(config.read_terms(), "")


class TermsEndpointTests(unittest.TestCase):
    def setUp(self):
        isolated_data(self)
        self.cfg = dict(config.DEFAULTS)
        self.state = Gateway(self.cfg, Mock(owned=False, url="http://127.0.0.1:9"))
        self.addCleanup(self.state.close)
        self.server = Server(("127.0.0.1", 0), self.state)
        self.cfg["gateway_port"] = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_read_and_publish_the_shared_glossary(self):
        empty = client.request_json(self.cfg, "/admin/terms")
        self.assertEqual((empty["terms"], empty["entries"]), ("", 0))
        session = client.request_json(self.cfg, "/sessions/open", {})
        self.assertEqual(session["terms_revision"], empty["revision"])
        saved = client.request_json(self.cfg, "/admin/terms", {"terms": "[展会]\nSpritzguss=注塑"})
        self.assertEqual(saved["entries"], 1)
        self.assertNotEqual(saved["revision"], empty["revision"])
        self.assertEqual(config.read_terms(), "[展会]\nSpritzguss=注塑")
        beat = client.request_json(self.cfg, "/sessions/heartbeat", {"session": session["session"]})
        self.assertEqual(beat["terms_revision"], saved["revision"])
        self.assertEqual(client.request_json(self.cfg, "/admin/terms")["terms"], "[展会]\nSpritzguss=注塑")
        self.assertEqual(self.state.status_report()["terms"], {"entries": 1, "revision": saved["revision"]})
        with self.assertRaisesRegex(RuntimeError, "400"):
            client.request_json(self.cfg, "/admin/terms", {"terms": 3})
        client.request_json(self.cfg, "/sessions/close", {"session": session["session"]})

    def test_other_devices_cannot_read_or_change_the_glossary(self):
        self.cfg["gateway_lan"] = True
        token = access.add_device("mac")
        with patch.object(Handler, "_peer", return_value=ipaddress.ip_address("192.168.1.20")):
            for method, body in (("GET", None), ("POST", json.dumps({"terms": "x"}))):
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
                conn.request(method, "/admin/terms", body=body,
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                self.assertEqual(conn.getresponse().status, 403, method)
                conn.close()
        self.assertEqual(config.read_terms(), "")


class IndependenceTests(unittest.TestCase):
    def test_health_names_the_service_and_older_gateways_are_recognised(self):
        with patch.object(client, "request_json", return_value={"service": "audio-translation", "stopping": False}):
            self.assertTrue(client.status({}))
        with patch.object(client, "request_json", return_value={"service": "something-else"}):
            with self.assertRaises(RuntimeError):
                client.status({})

    def test_gateway_imports_only_the_standard_library_and_filelock(self):
        allowed = set(sys.stdlib_module_names) | {"filelock", "translation_gateway"}
        for source in PACKAGE.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{source.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
