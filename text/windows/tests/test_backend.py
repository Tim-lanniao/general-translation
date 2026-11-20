"""The desk's gateway client, audio control and component boundary, without Qt."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from translation_desk import backend

PACKAGE = Path(backend.__file__).resolve().parent


class FakeGateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json(200, self.server.status_body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, body))
        if "fail" in json.dumps(body):
            self._json(503, {"error": {"message": "GPU 正由其他转写任务使用"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for word in self.server.words:
                self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {"content": word}}]}).encode() + b"\n\n")
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": self.server.finish}]}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        except ConnectionError:  # the client cancelled
            pass


class GatewayClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        self.server.calls, self.server.finish = [], "stop"
        self.server.words = ["译", "文：", "会议", "改到", "周二。"]
        self.server.status_body = {"service": "translation-gateway", "caption_sessions": 0,
                                   "model": {"id": "qwen", "label": "Qwen", "state": "ready"}}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.gateway = backend.Gateway(f"http://127.0.0.1:{self.server.server_port}")

    def test_translation_streams_clean_partials(self):
        partials = []
        text, finish = self.gateway.translate("Der Termin ist am Dienstag.", "de", context="Vorher.", on_partial=partials.append)
        self.assertEqual((text, finish), ("会议改到周二。", "stop"))
        self.assertEqual(partials, ["会议", "会议改到", "会议改到周二。"])  # the model's "译文：" label never shows
        path, body = self.server.calls[-1]
        self.assertEqual(path, "/v1/translate")
        self.assertEqual(body, {"source": "Der Termin ist am Dienstag.", "source_language": "de", "context": "Vorher.", "stream": True})

    def test_other_targets_use_the_chat_interface_with_an_instruction(self):
        self.gateway.translate("明天见。", target="de")
        path, body = self.server.calls[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["messages"][1], {"role": "user", "content": "明天见。"})
        self.assertIn("德语", body["messages"][0]["content"])

    def test_warming_bypasses_the_cache(self):
        self.gateway.warm()
        self.assertGreater(self.server.calls[-1][1]["temperature"], 0)

    def test_errors_are_readable(self):
        with self.assertRaisesRegex(backend.DeskError, "503：GPU 正由其他转写任务使用"):
            self.gateway.translate("fail")
        with self.assertRaises(CancelledError):
            self.gateway.translate("Hallo", cancelled=lambda: True)
        self.server.finish = "length"
        self.assertEqual(self.gateway.translate("Hallo")[1], "length")
        closed = backend.Gateway("http://127.0.0.1:9")
        self.assertIsNone(closed.status())
        with self.assertRaisesRegex(backend.DeskError, "无法连接翻译网关"):
            closed.translate("Hallo")

    def test_status_only_accepts_the_translation_gateway(self):
        self.assertEqual(backend.describe(self.gateway.status()), ("ready", "文本翻译就绪 · Qwen"))
        self.server.status_body = {"service": "something-else"}
        self.assertIsNone(self.gateway.status())
        self.assertEqual(backend.describe(None)[0], "offline")
        self.assertIn("空闲时已卸载", backend.describe({"model": {"state": "unloaded"}})[1])

    @unittest.skipUnless(sys.platform == "win32", "the gateway launcher is a .cmd file")
    def test_start_runs_the_launcher_and_takes_its_address(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "translation-gateway.cmd"
            launcher.write_text("@echo off\r\nif \"%1\"==\"start\" echo http://127.0.0.1:19999\r\n", encoding="ascii")
            gateway = backend.Gateway("http://127.0.0.1:9", launcher=launcher)  # nothing answers there yet
            self.assertEqual(gateway.start(), "http://127.0.0.1:19999")
            launcher.write_text("@echo off\r\necho Python environment not found: x\r\necho Create one in this folder\r\n"
                                "exit /b 1\r\n", encoding="ascii")
            gateway.url = "http://127.0.0.1:9"
            with self.assertRaisesRegex(backend.DeskError, "Python environment not found: x Create one in this folder"):
                gateway.start()
        with self.assertRaisesRegex(backend.DeskError, "找不到翻译网关启动器"):
            backend.Gateway("http://127.0.0.1:9", launcher=Path(folder) / "missing.cmd").start()
        # A gateway that already answers is used as it is, whatever the launcher would do.
        self.assertEqual(backend.Gateway(self.gateway.url, launcher=Path(folder) / "missing.cmd").start(), self.gateway.url)


class HelperTests(unittest.TestCase):
    def test_long_text_is_split_into_paragraphs_and_sentences(self):
        self.assertEqual(backend.split_paragraphs("Erster Absatz.\n\n  \nZweiter Absatz."), ["Erster Absatz.", "Zweiter Absatz."])
        long = "Ein ziemlich langer Satz über die Wartung der Maschine. " * 40
        pieces = backend.split_paragraphs(long, limit=300)
        self.assertTrue(all(len(p) <= 300 for p in pieces))
        self.assertTrue(all(p.endswith(".") for p in pieces[:-1]))
        self.assertEqual(" ".join(pieces), long.strip())

    def test_audio_is_controlled_through_its_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / "pythonw.exe"
            python.touch()
            audio = backend.Audio(directory=Path(folder), python=str(python))
            with patch("translation_desk.backend.subprocess.Popen") as popen:
                audio.open()
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(python), "-m", "audio_caption"])
            self.assertEqual((kwargs["cwd"], kwargs["creationflags"]), (Path(folder), backend.DETACHED))
            with patch("translation_desk.backend.subprocess.run") as run:
                audio.close()
            self.assertEqual(run.call_args.args[0], [str(python), "-m", "audio_caption", "--quit"])
            with patch("translation_desk.backend.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
                with self.assertRaises(backend.DeskError):
                    audio.close()
        with self.assertRaisesRegex(backend.DeskError, "找不到音频翻译的 Python 环境"):
            backend.Audio(directory=Path(folder), python=str(Path(folder) / "none.exe")).open()
        self.assertTrue(backend.Audio.running({"caption_sessions": 1}))
        self.assertFalse(backend.Audio.running({"caption_sessions": 0}))
        self.assertFalse(backend.Audio.running(None))

    def test_a_relative_data_root_becomes_absolute(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, TRANSLATION_DATA="relative-data", PYTHONPATH=str(PACKAGE.parent))
            out = subprocess.run([sys.executable, "-c", "from translation_desk import paths; print(paths.DATA_DIR)"],
                                 cwd=folder, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(Path(out.stdout.strip()), Path(folder).resolve() / "relative-data" / "text")

    def test_the_desk_imports_neither_the_gateway_nor_the_audio_app(self):
        for source in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"translation_gateway", "audio_caption"}, source.name)


if __name__ == "__main__":
    unittest.main()
