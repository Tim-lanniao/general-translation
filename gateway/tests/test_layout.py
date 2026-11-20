"""Data root, the shared-glossary endpoint and the gateway's independence from other components."""
import ast
import http.client
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import isolated_data
from translation_gateway import access, client, config
from translation_gateway.cache import TranslationCache
from translation_gateway.server import Gateway, Handler, Server

PACKAGE = Path(config.__file__).resolve().parent


class DataRootTests(unittest.TestCase):
    def test_a_relative_data_root_becomes_absolute(self):
        """A gateway started by a client runs in gateway/ and must still find the client's data root."""
        with tempfile.TemporaryDirectory() as folder:
            code = "from translation_gateway import config; print(config.ROOT)"
            env = dict(os.environ, TRANSLATION_DATA="relative-data", PYTHONPATH=str(PACKAGE.parent))
            out = subprocess.run([sys.executable, "-c", code], cwd=folder, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(Path(out.stdout.strip()), Path(folder).resolve() / "relative-data")

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

    def test_a_large_glossary_can_be_saved_back(self):
        raw = "\n".join(f"Bauteil{i:05d}=零件{i:05d}" for i in range(12000))  # about 300 KB, above a request's limit
        self.assertGreater(len(raw.encode("utf-8")), 128 * 1024)
        self.assertEqual(client.request_json(self.cfg, "/admin/terms", {"terms": raw})["entries"], 12000)
        self.assertEqual(client.request_json(self.cfg, "/admin/terms")["terms"], raw)

    def test_cache_keeps_its_limit_across_restarts(self):
        path = config.DATA_DIR / "cache" / "limit.sqlite3"
        cache = TranslationCache(path, 100)
        for i in range(150):  # fewer than the 200 writes that trigger pruning during a run
            cache.put(f"key{i}", "译文", "model")
        self.assertEqual(cache.count(), 150)
        cache.close()
        cache = TranslationCache(path, 100)
        self.addCleanup(cache.close)
        self.assertEqual(cache.count(), 100)
        self.assertEqual(cache.get("key149"), "译文")

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
    def test_unavailable_explicit_endpoint_never_spawns_a_second_gateway(self):
        with patch.object(client, "status", return_value=None), patch.object(subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "指定的翻译网关"):
                client.ensure_gateway({"gateway_url": "https://localhost:11111"})
            spawn.assert_not_called()

    def test_clients_only_accept_this_gateway_on_the_port(self):
        with patch.object(client, "request_json", return_value={"service": "translation-gateway", "stopping": False}):
            self.assertTrue(client.status({}))
        for other in ("audio-translation", "something-else"):
            with patch.object(client, "request_json", return_value={"service": other}):
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
