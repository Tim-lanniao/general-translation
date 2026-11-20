"""LAN clients, browser extensions, slot reservation, cancellation, cache and status of the gateway."""
import contextlib
import http.client
import io
import ipaddress
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from teams_caption import config, gateway_access
from teams_caption.gateway import Gateway, Handler, Server, main
from teams_caption.translate import _stream_chat, gateway_url, request_json

REMOTE = ipaddress.ip_address("192.168.1.20")
PUBLIC = ipaddress.ip_address("203.0.113.9")


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(.01)
    if not condition():
        raise AssertionError("timed out waiting for test milestone")


class Model(BaseHTTPRequestHandler):
    """Streams like an OpenAI-compatible server. "hold" waits, "slow" streams 100 tokens."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["messages"][-1]["content"]
        with server.lock:
            server.calls.append(body)
            server.inflight += 1
            server.peak = max(server.peak, server.inflight)
        try:
            if "hold" in text:
                server.unblock.wait(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ["词"] * 100 if "slow" in text else ["第", "一句"]:
                self.wfile.write(b"data: " + json.dumps({"id": "up-1", "choices": [{"delta": {"content": word}}]}).encode() + b"\n\n")
                self.wfile.flush()
                if "slow" in text:
                    time.sleep(.03)
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        except ConnectionError:  # Windows reports a closed peer as ConnectionAbortedError
            server.aborted.set()
        finally:
            with server.lock:
                server.inflight -= 1


class LanGatewayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        for key, value in (("DATA_DIR", self.folder), ("CONFIG_PATH", self.folder / "config.json")):
            p = patch.object(config, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        self.model.calls, self.model.inflight, self.model.peak = [], 0, 0
        self.model.lock, self.model.unblock, self.model.aborted = threading.Lock(), threading.Event(), threading.Event()
        self._serve(self.model)
        self.runtime = Mock(owned=False, url=f"http://127.0.0.1:{self.model.server_port}")

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def start_gateway(self, **overrides):
        self.cfg = dict(config.DEFAULTS, gateway_idle_seconds=60, **overrides)
        self.state = Gateway(self.cfg, self.runtime)
        self.addCleanup(self.state.close)
        self.addCleanup(self.model.unblock.set)
        self.server = Server(("127.0.0.1", 0), self.state)
        self.cfg["gateway_port"] = self.server.server_port
        self._serve(self.server)

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
            conn.request(method, path, body=data, headers={**({"Content-Type": "application/json"} if data else {}), **(headers or {})})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def post(self, path, body):
        return request_json(self.cfg, path, body)

    def test_lan_clients_need_a_device_token_and_only_reach_text_endpoints(self):
        self.start_gateway(gateway_lan=True)
        with patch.object(Handler, "_peer", return_value=REMOTE):
            status, _, data = self.call("POST", "/v1/translate", {"source": "Guten Tag"})
            self.assertEqual(status, 401, data)
            token = gateway_access.add_device("mac")
            auth = {"Authorization": f"Bearer {token}"}
            status, _, data = self.call("POST", "/v1/translate", {"source": "Guten Tag"}, auth)
            self.assertEqual(status, 200, data)
            self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "第一句")
            self.assertEqual(self.call("GET", "/v1/models", headers=auth)[0], 200)
            for path in ("/v1/captions", "/sessions/open", "/admin/stop"):
                self.assertEqual(self.call("POST", path, {"source": "x"}, auth)[0], 403, path)
            self.assertFalse(self.state.stopping)
            self.assertEqual(self.call("POST", "/v1/translate", {"source": "x"}, {"Authorization": "Bearer wrong"})[0], 401)
            self.assertTrue(gateway_access.revoke_device("mac"))
            self.assertEqual(self.call("POST", "/v1/translate", {"source": "Guten Tag"}, auth)[0], 401)
        token = gateway_access.add_device("phone")
        with patch.object(Handler, "_peer", return_value=PUBLIC):
            status, _, _ = self.call("POST", "/v1/translate", {"source": "Guten Tag"}, {"Authorization": f"Bearer {token}"})
            self.assertEqual(status, 403)
        self.assertEqual(self.state.status_report()["clients"]["mac"]["text"]["requests"], 1)
        # This PC keeps working without a token, and a device token on this PC is attributed to the device.
        self.assertEqual(self.call("POST", "/v1/translate", {"source": "lokal"})[0], 200)
        self.assertEqual(self.call("POST", "/v1/translate", {"source": "lokal 2"}, {"Authorization": f"Bearer {token}"})[0], 200)
        self.assertIn("phone", self.state.status_report()["clients"])

    def test_cors_preflight_for_extensions_and_lan_origins(self):
        self.start_gateway(gateway_lan=True)
        preflight = {"Origin": "chrome-extension://abcdef", "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization, content-type, x-trace"}
        status, headers, _ = self.call("OPTIONS", "/v1/chat/completions", headers=preflight)
        self.assertEqual(status, 204)
        self.assertEqual(headers["access-control-allow-origin"], "chrome-extension://abcdef")
        self.assertIn("Authorization", headers["access-control-allow-headers"])
        self.assertIn("x-trace", headers["access-control-allow-headers"])
        body = {"messages": [{"role": "user", "content": "Hallo"}]}
        status, headers, _ = self.call("POST", "/v1/chat/completions", body, {"Origin": "chrome-extension://abcdef"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "chrome-extension://abcdef")
        for method in ("OPTIONS", "POST"):
            status, headers, _ = self.call(method, "/v1/chat/completions", body if method == "POST" else None,
                                           {"Origin": "https://evil.example"})
            self.assertEqual(status, 403, method)
            self.assertNotIn("access-control-allow-origin", headers)
        with patch.object(Handler, "_peer", return_value=REMOTE):
            status, headers, _ = self.call("OPTIONS", "/v1/chat/completions", headers={
                "Origin": "https://reader.example", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true"})
            self.assertEqual(status, 204)
            self.assertEqual(headers["access-control-allow-origin"], "https://reader.example")
            self.assertEqual(headers["access-control-allow-private-network"], "true")
            status, headers, _ = self.call("POST", "/v1/chat/completions", body, {"Origin": "https://reader.example"})
            self.assertEqual(status, 401)
            self.assertEqual(headers["access-control-allow-origin"], "https://reader.example")

    def test_parallel_model_keeps_one_slot_for_captions(self):
        self.start_gateway(llm_parallel=2)
        session = self.state.session("open", None)["session"]
        self.addCleanup(self.state.session, "close", session)
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(self.post, "/v1/translate", {"source": "hold A"})
            wait_for(lambda: len(self.model.calls) == 1)
            second = pool.submit(self.post, "/v1/translate", {"source": "hold B"})
            wait_for(lambda: self.state.info()["queued_text"] == 1)
            live = pool.submit(_stream_chat, gateway_url(self.cfg) + "/v1/captions",
                               {"source": "Die Presse hold", "session": session, "stream": True})
            wait_for(lambda: len(self.model.calls) == 2)
            self.assertEqual(self.state.info()["queued_text"], 1)
            self.assertEqual(self.state.status_report()["running"], {"caption": 1, "text": 1})
            self.model.unblock.set()
            self.assertEqual(live.result(5)[0], "第一句")
            for request in (first, second):
                self.assertEqual(request.result(5)["choices"][0]["message"]["content"], "第一句")
        self.assertEqual(self.model.peak, 2)
        self.assertEqual([c["messages"][-1]["content"].splitlines()[-1] for c in self.model.calls],
                         ["hold A", "Die Presse hold", "hold B"])

    def test_client_disconnect_stops_generation_and_frees_the_slot(self):
        self.start_gateway()
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.model.aborted.clear()
                before = len(self.model.calls)
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
                conn.request("POST", "/v1/translate", body=json.dumps({"source": f"slow {stream}", "stream": stream}),
                             headers={"Content-Type": "application/json"})
                wait_for(lambda: len(self.model.calls) == before + 1)
                time.sleep(.15)
                conn.close()
                self.assertTrue(self.model.aborted.wait(3), "model kept generating for a closed client")
                wait_for(lambda: self.state.info()["active"] is None)
        self.assertEqual(self.post("/v1/translate", {"source": "danach"})["choices"][0]["message"]["content"], "第一句")
        report = self.state.status_report()
        self.assertEqual(report["clients"]["local"]["text"]["cancelled"], 2)
        self.assertEqual(report["cache"]["entries"], 1)  # only the completed request

    def test_cache_serves_repeats_without_the_model(self):
        self.start_gateway()
        request = {"source": "Guten Tag", "source_language": "de"}
        first = self.post("/v1/translate", request)
        self.assertEqual(len(self.model.calls), 1)
        self.state.loaded = False  # as after an idle unload
        self.runtime.start.reset_mock()
        status, headers, data = self.call("POST", "/v1/translate", request)
        self.assertEqual((status, headers["x-gateway-cache"]), (200, "hit"))
        self.assertEqual(json.loads(data)["choices"], first["choices"])
        text, _, finish = _stream_chat(gateway_url(self.cfg) + "/v1/translate", dict(request, stream=True))
        self.assertEqual((text, finish), ("第一句", "stop"))
        self.runtime.start.assert_not_called()
        self.assertEqual(len(self.model.calls), 1)
        config.save_shared_terms("Tag=日子")  # a term in this sentence changes the prompt
        self.post("/v1/translate", request)
        self.assertEqual(len(self.model.calls), 2)
        config.save_shared_terms("Tag=日子\nPresse=压力机")  # unrelated term: still a hit
        self.post("/v1/translate", request)
        self.assertEqual(len(self.model.calls), 2)
        for _ in range(2):
            self.post("/v1/translate", dict(request, temperature=.7))
        self.assertEqual(len(self.model.calls), 4)
        session = self.state.session("open", None)["session"]
        for _ in range(2):
            _stream_chat(gateway_url(self.cfg) + "/v1/captions", {"source": "Guten Tag", "session": session, "stream": True})
        self.state.session("close", session)
        self.assertEqual(len(self.model.calls), 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["cache-clear"]), 0)
        self.assertIn("已清除 2 条", out.getvalue())
        self.post("/v1/translate", request)
        self.assertEqual(len(self.model.calls), 7)

    def test_status_reports_model_queues_and_clients_without_text(self):
        self.start_gateway()
        for _ in range(2):
            self.post("/v1/translate", {"source": "Guten Morgen"})
        status, _, data = self.call("GET", "/status")
        self.assertEqual(status, 200)
        report = json.loads(data)
        self.assertEqual((report["model"]["state"], report["model"]["parallel"]), ("ready", 1))
        self.assertEqual(report["queued"], {"captions": 0, "text": 0})
        self.assertEqual(report["cache"], {"enabled": True, "entries": 1})
        self.assertEqual((report["network"]["lan"], report["network"]["devices"]), (False, 0))
        local = report["clients"]["local"]["text"]
        self.assertEqual((local["requests"], local["cache_hits"], local["cache_hit_rate"]), (2, 1, .5))
        self.assertIsNotNone(local["first_token_p50_s"])
        self.assertNotIn("Guten", data.decode())

    def test_device_token_commands(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["token-add", "iphone"]), 0)
            for bad in (["token-add", "iphone"], ["token-add", "local"], ["token-add", "zwei worte"], ["token-add"]):
                self.assertEqual(main(bad), 1, bad)
            self.assertEqual(main(["token-list"]), 0)
        token = out.getvalue().splitlines()[1]
        self.assertIn("iphone", out.getvalue().splitlines()[-1])
        self.assertEqual(gateway_access.DeviceRegistry().lookup(token), "iphone")
        self.assertNotIn(token, gateway_access.devices_path().read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["token-revoke", "iphone"]), 0)
            self.assertEqual(main(["token-revoke", "iphone"]), 1)
        self.assertIsNone(gateway_access.DeviceRegistry().lookup(token))

    def test_network_policy(self):
        peer, allowed, parse = gateway_access.peer_address, gateway_access.remote_allowed, gateway_access.parse_networks
        self.assertTrue(allowed(peer("192.168.1.20"), []))
        self.assertTrue(allowed(peer("::ffff:10.0.0.7"), []))
        self.assertFalse(allowed(peer("203.0.113.9"), []))
        self.assertFalse(allowed(peer("8.8.8.8"), []))
        self.assertTrue(allowed(peer("fd12:3456::1"), []))
        self.assertFalse(allowed(peer("127.0.0.1"), []))
        networks = parse(["100.64.0.0/10"])
        self.assertTrue(allowed(peer("100.101.1.2"), networks))
        self.assertFalse(allowed(peer("192.168.1.20"), networks))
        with self.assertRaises(RuntimeError):
            parse(["nonsense"])


if __name__ == "__main__":
    unittest.main()
