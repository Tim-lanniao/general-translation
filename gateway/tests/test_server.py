import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from filelock import Timeout

from support import isolated_data
from translation_gateway import config
from translation_gateway.client import gateway_url, request_json, stream_chat
from translation_gateway.server import Gateway, Server, mode_lock, test_control as control


def wait_for(condition):
    end = time.monotonic() + 5
    while not condition() and time.monotonic() < end:
        time.sleep(.01)
    if not condition():
        raise AssertionError("timed out waiting for test milestone")


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(body)
        if "hold" in body["messages"][-1]["content"]:
            self.server.unblock.wait(5)
        if "error" in body["messages"][-1]["content"]:
            self.send_error(503)
            return
        self.send_response(200)
        self.end_headers()
        if body["stream"]:
            for word in ("第", "一句"):
                event = {"choices": [{"delta": {"content": word}}]}
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(json.dumps({"choices": [{"message": {"content": "第一句"}}]}).encode())


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.folder = isolated_data(self)
        self.folder.mkdir(parents=True)
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.up.calls = []
        self.up.unblock = threading.Event()
        self.runtime = Mock(owned=False, url=f"http://127.0.0.1:{self.up.server_port}")
        self.cfg = dict(config.DEFAULTS, gateway_idle_seconds=.05, gpu_lock=str(self.folder / "shared.lock"))
        self.state = Gateway(self.cfg, self.runtime)
        self.addCleanup(self.state.close)
        self.addCleanup(self.state.unload)
        self.server = Server(("127.0.0.1", 0), self.state)
        self.cfg["gateway_port"] = self.server.server_port
        for server in (self.up, self.server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def post(self, path, body):
        return request_json(self.cfg, path, body)

    def test_mixed_clients_one_slot_streaming_and_error_recovery(self):
        config.save_terms("Presse=压力机\nGX-100=样机")
        session = self.post("/sessions/open", {})["session"]
        self.addCleanup(self.state.session, "close", session)
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(self.post, "/v1/translate", {"source": "hold"})
            wait_for(lambda: len(self.up.calls) == 1)
            bulk = pool.submit(self.post, "/v1/chat/completions", {"messages": [{"role": "user", "content": [{"type": "text", "text": "third"}]}],
                               "chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "high"})
            wait_for(lambda: self.state.info()["queued_text"] == 1)
            previews = []
            live = pool.submit(stream_chat, gateway_url(self.cfg) + "/v1/captions",
                               {"source": "Die Presse", "session": session, "stream": True}, on_partial=previews.append)
            wait_for(lambda: self.state.info()["queued_captions"] == 1)
            self.assertEqual(len(self.up.calls), 1)
            self.up.unblock.set()
            self.assertEqual(live.result()[0], "第一句")
            self.assertEqual(previews, ["第", "第一句"])
            self.assertEqual(first.result()["choices"], bulk.result()["choices"])
        order = [p["messages"][-1]["content"] for p in self.up.calls]
        self.assertEqual([x.splitlines()[-1] for x in order], ["hold", "Die Presse", "third"])
        self.assertIn("Presse=压力机", self.up.calls[1]["messages"][0]["content"])
        self.assertNotIn("GX-100", str(self.up.calls))
        self.assertNotIn("reasoning_effort", self.up.calls[2])
        self.assertTrue(all(p["chat_template_kwargs"] == {"enable_thinking": False} for p in self.up.calls))
        with self.assertRaisesRegex(RuntimeError, "503"):
            self.post("/v1/translate", {"source": "error"})
        config.save_terms("Presse=压机")
        self.post("/v1/translate", {"source": "Presse"})
        self.assertIn("Presse=压机", self.up.calls[-1]["messages"][0]["content"])

    def test_session_close_idle_unload_and_reload(self):
        session = self.post("/sessions/open", {})["session"]
        self.assertTrue(self.state.gpu.is_locked)
        stream_chat(gateway_url(self.cfg) + "/v1/captions", {"source": "Hallo", "session": session, "stream": True})
        with self.assertRaisesRegex(RuntimeError, "409"):
            self.post("/admin/stop", {})
        self.post("/sessions/close", {"session": session})
        self.runtime.stop.assert_not_called()
        self.assertTrue(self.state.info()["loaded"])
        monitor = threading.Thread(target=self.state.maintain)
        monitor.start()
        try:
            wait_for(lambda: not self.state.info()["loaded"])
            self.assertFalse(self.state.gpu.is_locked)
            self.runtime.stop.assert_called_once()
            self.post("/v1/translate", {"source": "Weiter"})
            self.assertEqual(self.runtime.start.call_count, 2)
        finally:
            self.state.request_stop()
            monitor.join(2)

    def test_pending_bulk_limit_and_withdrawn_connection(self):
        session = self.state.session("open", None)["session"]
        dropped = threading.Event()
        def wait_for_slot():
            with self.state.slot(False, disconnected=dropped.is_set):
                self.fail("cancelled text request entered inference")
        def caption_slot():
            with self.state.slot(True, session):
                return True
        with ThreadPoolExecutor(2) as pool:
            with self.state.slot(False):
                pending = pool.submit(wait_for_slot)
                wait_for(lambda: self.state.info()["queued_text"] == 1)
                # Fill the remaining admission tickets while inference is busy.
                with self.state.cv:
                    self.state.bulk.extend(object() for _ in range(31))
                with self.assertRaisesRegex(RuntimeError, "429"):
                    self.post("/v1/translate", {"source": "overflow"})
                priority = pool.submit(caption_slot)
                wait_for(lambda: self.state.info()["queued_captions"] == 1)
                dropped.set()
                with self.assertRaises(ConnectionAbortedError):
                    pending.result(2)
                with self.state.cv:
                    self.state.bulk.clear()
            self.assertTrue(priority.result(2))
        self.state.session("close", session)

    def test_exclusive_mode_and_failed_stop_recovery(self):
        cfg = dict(self.cfg, llm_controller="configured")
        runtime = Mock()
        with patch("translation_gateway.server.ModelRuntime", return_value=runtime):
            with mode_lock(), self.assertRaises(Timeout):
                control(cfg, "test-start")
            with self.assertRaises(RuntimeError):
                control(cfg, "test-check")
            runtime.ready.side_effect = [False, True]
            control(cfg, "test-start")
            control(cfg, "test-check")
            runtime.ready.side_effect = [True]
            with self.assertRaises(RuntimeError):
                control(cfg, "test-stop")
            self.assertTrue((self.folder / "model-test.active").exists())
            runtime.ready.side_effect = [False]
            control(cfg, "test-stop")
            self.assertFalse((self.folder / "model-test.active").exists())


if __name__ == "__main__":
    unittest.main()
