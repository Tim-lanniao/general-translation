"""Audio settings, capture selection and the caption client against a real gateway."""
import ast
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import audio_caption  # noqa: F401  (puts ../gateway on sys.path when run from a checkout)
from audio_caption import config, translator
from audio_caption.capture import LoopbackCapture
from audio_caption.engine import source_spec
from audio_caption.translator import GatewayTranslator
from translation_gateway import config as gateway_config
from translation_gateway.server import Gateway, Server

PACKAGE = Path(config.__file__).resolve().parent
GATEWAY_INTERFACE = {"translation_gateway", "translation_gateway.client", "translation_gateway.config", "translation_gateway.terms"}


def isolated_data(case) -> Path:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for module, values in ((gateway_config, {"ROOT": root, "DATA_DIR": root / "gateway", "CONFIG_PATH": root / "gateway" / "config.json",
                                             "LOG_DIR": root / "gateway" / "logs"}),
                           (config, {"DATA_DIR": root / "audio", "CONFIG_PATH": root / "audio" / "config.json",
                                     "RECORD_DIR": root / "audio" / "会议记录", "LOG_DIR": root / "audio" / "logs"})):
        for key, value in values.items():
            p = patch.object(module, key, value)
            p.start()
            case.addCleanup(p.stop)
    return root


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for word in ("第", "一句"):
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {"content": word}}]}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


class AudioSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_data(self)
        (self.root / "audio").mkdir()

    def test_audio_default_source_and_translator(self):
        config.CONFIG_PATH.write_text(json.dumps({"source": "device:default", "translator": "hymt7b"}))
        cfg = config.load()
        self.assertEqual((cfg["source"], cfg["translator"]), ("system", "qwen"))
        config.CONFIG_PATH.write_text(json.dumps({"source": "device:custom", "terms": "Messe"}))
        self.assertEqual(config.load()["source"], "device:custom")
        self.assertNotIn("llm_url", config.load())  # model settings belong to the gateway

    def test_system_capture_excludes_this_process_and_falls_back_to_a_device(self):
        capture = LoopbackCapture(source_spec("system"), Mock(), Mock())
        with patch("audio_caption.capture._activate_process_loopback", return_value=17) as activate:
            self.assertEqual(capture._open()[::2], (17, os.getpid()))
            activate.assert_called_once_with(os.getpid(), exclude=True)
        with patch("audio_caption.capture._activate_process_loopback", side_effect=OSError), \
                patch("audio_caption.capture._activate_device", return_value=(23, "speakers")):
            self.assertEqual(capture._open()[::2], (23, None))


class CaptionClientTests(unittest.TestCase):
    def setUp(self):
        isolated_data(self)
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        self.model.calls = []
        self.gateway_cfg = dict(gateway_config.DEFAULTS, llm_label="Testmodell")
        self.state = Gateway(self.gateway_cfg, Mock(owned=False, url=f"http://127.0.0.1:{self.model.server_port}"))
        self.addCleanup(self.state.close)
        self.server = Server(("127.0.0.1", 0), self.state)
        self.gateway_cfg["gateway_port"] = self.server.server_port
        for server in (self.model, self.server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        for target in ("translation_gateway.client.ensure_gateway", "audio_caption.translator.gateway_config.load"):
            p = patch(target, return_value=self.gateway_cfg) if target.endswith("load") else patch(target)
            p.start()
            self.addCleanup(p.stop)

    def test_session_translation_and_shared_terms(self):
        client = GatewayTranslator({"asr_language": "de"})
        self.assertEqual(client.label, "Testmodell")
        client.start(lambda _: None)
        self.assertEqual(self.state.info()["caption_sessions"], 1)
        first = client.terms_revision
        self.assertIsNotNone(first)
        previews = []
        result = client.translate("Die Presse", "", "展会", asr_names=["Presse"], on_partial=previews.append)
        self.assertEqual((result["translation"], previews), ("第一句", ["第", "第一句"]))
        self.assertEqual(self.model.calls[-1]["messages"][-1]["content"], "当前语句：\nDie Presse")

        saved = translator.save_shared_terms("Presse=压力机")
        self.assertEqual(translator.read_shared_terms()["terms"], "Presse=压力机")
        self.assertNotEqual(saved["revision"], first)
        client.translate("Die Presse", "", "")
        self.assertIn("Presse=压力机", self.model.calls[-1]["messages"][0]["content"])
        client._call("/sessions/heartbeat", {"session": client.session})
        self.assertEqual(client.terms_revision, saved["revision"])
        client.stop()
        self.assertEqual(self.state.info()["caption_sessions"], 0)

    def test_explicit_endpoint_owns_the_session_even_with_another_configured_port(self):
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.gateway_cfg["gateway_port"] = 9
        with patch.dict(os.environ, TRANSLATION_GATEWAY_URL=url):
            captions = GatewayTranslator({"asr_language": "de"})
            captions.start(lambda _: None)
            try:
                self.assertEqual(self.state.info()["caption_sessions"], 1)
                self.assertEqual(captions.translate("Hallo", "", "")["translation"], "第一句")
                self.assertIn("revision", translator.read_shared_terms())
            finally:
                captions.stop()


class BoundaryTests(unittest.TestCase):
    def test_audio_uses_only_the_gateway_client_interface(self):
        for source in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module.startswith("translation_gateway"):
                    used = ({f"translation_gateway.{a.name}" for a in node.names} if node.module == "translation_gateway"
                            else {node.module})
                elif isinstance(node, ast.Import):
                    used = {a.name for a in node.names if a.name.startswith("translation_gateway")}
                else:
                    continue
                self.assertLessEqual(used, GATEWAY_INTERFACE, source.name)


if __name__ == "__main__":
    unittest.main()
