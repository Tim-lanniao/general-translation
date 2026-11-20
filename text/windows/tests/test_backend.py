"""The desk's gateway client, audio control and component boundary, without Qt."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from translation_desk import backend, paths

PACKAGE = Path(backend.__file__).resolve().parent


class FakeGateway(BaseHTTPRequestHandler):
    """Streams like the gateway. "half" breaks off mid-answer, "empty" answers nothing, "hold" waits first."""

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
        self.server.calls.append((self.path, body, dict(self.headers)))
        text = json.dumps(body, ensure_ascii=False)
        if "fail" in text:
            self._json(503, {"error": {"message": "GPU 正由其他转写任务使用"}})
            return
        if "hold" in text and not self.server.release.wait(5):
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            words = ["half"] if "half" in text else [] if "empty" in text else self.server.words
            for word in words:
                self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {"content": word}}]}).encode() + b"\n\n")
            if "half" in text:
                return  # the connection closes without a finish reason or [DONE]
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": self.server.finish}]}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        except ConnectionError:  # the client cancelled
            self.server.aborted.set()


class GatewayClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        self.server.calls, self.server.finish = [], "stop"
        self.server.release, self.server.aborted = threading.Event(), threading.Event()
        self.server.words = ["译", "文：", "会议", "改到", "周二。"]
        self.server.status_body = {"service": "translation-gateway", "caption_sessions": 0,
                                   "model": {"id": "qwen", "label": "Qwen", "state": "ready"}}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.release.set)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.gateway = backend.Gateway(self.url)

    def test_translation_streams_clean_partials_and_stays_off_the_disk_cache(self):
        partials = []
        text, finish = self.gateway.translate("Der Termin ist am Dienstag.", "de", context="Vorher.", on_partial=partials.append)
        self.assertEqual((text, finish), ("会议改到周二。", "stop"))
        self.assertEqual(partials, ["会议", "会议改到", "会议改到周二。"])  # the model's 「译文：」 label never shows
        path, body, headers = self.server.calls[-1]
        self.assertEqual(path, "/v1/translate")
        self.assertEqual(body, {"source": "Der Termin ist am Dienstag.", "source_language": "de", "context": "Vorher.", "stream": True})
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_translation_that_breaks_off_is_an_error_not_a_result(self):
        for source in ("half", "empty"):
            with self.assertRaises(backend.DeskError) as caught:
                self.gateway.translate(source)
            self.assertTrue(caught.exception.interrupted, source)
        self.server.finish = "content_filter"
        with self.assertRaisesRegex(backend.DeskError, "content_filter"):
            self.gateway.translate("Hallo")
        self.server.finish = "length"
        self.assertEqual(self.gateway.translate("Hallo")[1], "length")

    def test_only_the_gateway_prompt_labels_are_removed(self):
        self.server.words = ["中文：", "汉语"]
        self.assertEqual(self.gateway.translate("Chinesisch: Hanyu")[0], "中文：汉语")
        self.server.words = ["译文：", "Guten Tag"]
        self.assertEqual(self.gateway.translate("你好", target="de")[0], "译文：Guten Tag")  # no labels in our own prompt

    def test_other_targets_name_the_chosen_source_language(self):
        bodies = []
        for source in ("de", "en", "auto"):
            self.gateway.translate("Gift", source, "en")
            bodies.append(self.server.calls[-1][1])
        self.assertEqual(self.server.calls[-1][0], "/v1/chat/completions")
        systems = [b["messages"][0]["content"] for b in bodies]
        self.assertIn("原文是德语", systems[0])
        self.assertIn("原文是英语", systems[1])
        self.assertNotIn("原文是", systems[2])
        self.assertEqual(bodies[0]["messages"][1], {"role": "user", "content": "Gift"})

    def test_cancel_works_while_the_model_has_not_answered(self):
        call, outcome = backend.Call(), {}

        def run():
            try:
                self.gateway.translate("hold", call=call)
            except CancelledError:
                outcome["cancelled"] = time.monotonic()

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while not self.server.calls and time.monotonic() < deadline:
            time.sleep(.01)
        cancelled = time.monotonic()
        call.cancel()
        thread.join(3)
        self.assertLess(outcome["cancelled"] - cancelled, 1)
        self.server.release.set()
        self.assertTrue(self.server.aborted.wait(3), "the connection stayed open")
        with self.assertRaises(CancelledError):  # a cancelled call never starts again
            self.gateway.translate("Hallo", call=call)

    def test_errors_are_readable_but_logged_without_their_text(self):
        with self.assertRaisesRegex(backend.DeskError, "503：GPU 正由其他转写任务使用") as caught:
            self.gateway.translate("fail")
        self.assertEqual(caught.exception.log_line(), "translate failed (HTTP 503)")
        self.assertFalse(caught.exception.interrupted)
        closed = backend.Gateway("http://127.0.0.1:9")
        self.assertIsNone(closed.status())
        with self.assertRaisesRegex(backend.DeskError, "无法连接翻译网关"):
            closed.translate("Hallo")

    def test_warming_bypasses_the_cache(self):
        self.gateway.warm()
        self.assertGreater(self.server.calls[-1][1]["temperature"], 0)

    def test_status_and_caption_state(self):
        self.assertEqual(backend.describe(self.gateway.status()), ("ready", "文本翻译就绪 · Qwen"))
        self.server.status_body = {"service": "something-else"}
        self.assertIsNone(self.gateway.status())
        self.assertEqual(backend.describe(None)[0], "offline")
        self.assertIn("空闲时已卸载", backend.describe({"model": {"state": "unloaded"}})[1])
        state = backend.caption_state
        self.assertEqual(state(None), "unknown")
        self.assertEqual(state({"caption_sessions": 0}), "stopped")
        self.assertEqual(state({"caption_sessions": 1}), "loading")
        self.assertEqual(state({"caption_sessions": 1, "caption_states": {"loading": 1}}), "loading")
        self.assertEqual(state({"caption_sessions": 2, "caption_states": {"loading": 1, "running": 1}}), "running")
        self.assertEqual(state({"caption_sessions": 1, "caption_states": {"paused": 1}}), "paused")

    @unittest.skipUnless(sys.platform == "win32", "the gateway launcher is a .cmd file")
    def test_the_launcher_decides_the_address_unless_one_is_given(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            os.environ.pop("TRANSLATION_GATEWAY_URL", None)
            launcher = Path(folder) / "translation-gateway.cmd"
            launcher.write_text(f"@echo off\r\nif \"%1\"==\"start\" echo {self.url}\r\n", encoding="ascii")
            gateway = backend.Gateway(launcher=launcher)
            self.assertFalse(gateway.resolved)  # another gateway might answer on the default port
            self.assertEqual(gateway.start(), self.url)
            self.assertTrue(gateway.resolved)
            launcher.write_text("@echo off\r\necho Python environment not found: x\r\necho Create one in this folder\r\n"
                                "exit /b 1\r\n", encoding="ascii")
            with self.assertRaisesRegex(backend.DeskError, "Python environment not found: x Create one in this folder"):
                backend.Gateway(launcher=launcher).start()
            # An address given explicitly is used as it is while a gateway answers there.
            self.assertEqual(backend.Gateway(self.url, launcher=Path(folder) / "missing.cmd").start(), self.url)
        with self.assertRaisesRegex(backend.DeskError, "找不到翻译网关启动器"):
            backend.Gateway(launcher=Path(folder) / "missing.cmd").start()


class HelperTests(unittest.TestCase):
    def test_long_paragraphs_are_cut_at_sentences_or_spaces_and_joined_again(self):
        pieces = backend.split_paragraphs("Erster Absatz.\n\n  \nZweiter Absatz.")
        self.assertEqual([(p.text, p.new_paragraph) for p in pieces], [("Erster Absatz.", True), ("Zweiter Absatz.", True)])
        long = "Ein ziemlich langer Satz über die Wartung der Maschine. " * 40
        pieces = backend.split_paragraphs(long, limit=300)
        self.assertTrue(all(len(p.text) <= 300 and p.text.endswith(".") for p in pieces))
        self.assertEqual([p.new_paragraph for p in pieces], [True] + [False] * (len(pieces) - 1))
        words = "Wartungsintervall " * 60  # no sentence end at all
        self.assertTrue(all(p.text.split() and all(w == "Wartungsintervall" for w in p.text.split())
                            for p in backend.split_paragraphs(words, limit=300)))
        parts = [backend.Piece("A1", True), backend.Piece("A2", False), backend.Piece("B", True)]
        self.assertEqual(backend.join_pieces(parts, ["甲", "乙", "丙"], "zh"), "甲乙\n\n丙")
        self.assertEqual(backend.join_pieces(parts, ["One.", "Two.", "Three."], "en"), "One. Two.\n\nThree.")

    def test_audio_is_controlled_through_its_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / "pythonw.exe"
            python.touch()
            audio = backend.Audio(directory=Path(folder), python=str(python))
            with patch("translation_desk.backend.subprocess.Popen") as popen:
                audio.open()
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(python), "-m", "audio_caption", "--start"])
            self.assertEqual((kwargs["cwd"], kwargs["creationflags"]), (Path(folder), backend.DETACHED))
            self.assertEqual(kwargs["env"]["TRANSLATION_DATA"], str(paths.ROOT))
            # An app started a moment ago may miss the first quit: ask again until that process has exited.
            audio.process = Mock()
            audio.process.wait.side_effect = [subprocess.TimeoutExpired("x", 1), None]
            with patch("translation_desk.backend.subprocess.run", return_value=Mock(returncode=0)) as run:
                audio.close(retry_after=.01)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0], [str(python), "-m", "audio_caption", "--quit"])
            self.assertIsNone(audio.process)
            with patch("translation_desk.backend.subprocess.run", return_value=Mock(returncode=1)):
                with self.assertRaisesRegex(backend.DeskError, "exit 1"):
                    audio.close()
            with patch("translation_desk.backend.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
                with self.assertRaises(backend.DeskError):
                    audio.close()
        with self.assertRaisesRegex(backend.DeskError, "找不到音频翻译的 Python 环境"):
            backend.Audio(directory=Path(folder), python=str(Path(folder) / "none.exe")).open()

    def test_children_get_an_absolute_data_root(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, TRANSLATION_DATA="relative-data", PYTHONPATH=str(PACKAGE.parent))
            code = "from translation_desk import backend, paths; print(paths.DATA_DIR); print(backend.child_env()['TRANSLATION_DATA'])"
            out = subprocess.run([sys.executable, "-c", code], cwd=folder, env=env, capture_output=True, text=True, check=True)
            data_dir, child_root = out.stdout.split()
            self.assertEqual(Path(data_dir), Path(folder).resolve() / "relative-data" / "text")
            self.assertEqual(Path(child_root), Path(folder).resolve() / "relative-data")

    def test_the_desk_imports_neither_the_gateway_nor_the_audio_app(self):
        for source in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"translation_gateway", "audio_caption"}, source.name)


if __name__ == "__main__":
    unittest.main()
