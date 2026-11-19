"""Loopback translation service and exclusive local-model test controls."""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from . import config
from .glossary import Glossary
from .model_runtime import ModelRuntime
from .translate import ensure_gateway, gateway_url, request_json, status

log = logging.getLogger(__name__)
LOCAL = {"localhost", "127.0.0.1", "::1"}


class ServiceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _string(body, key, default=""):
    value = body.get(key, default)
    if not isinstance(value, str):
        raise ServiceError(400, f"{key} 应为文本")
    return value


def translation_request(path, body, cfg, glossary):
    """Create the upstream request once the caller has a slot."""
    topic = _string(body, "topic", cfg.get("topic", ""))
    if path == "/v1/chat/completions":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ServiceError(400, "需要 messages")
        normalized = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "developer"):
                raise ServiceError(400, "需要带有 role 的文本消息")
            content = message.get("content")
            if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text"
                                                 and isinstance(p.get("text"), str) for p in content):
                content = "\n".join(p["text"] for p in content)
            if not isinstance(content, str):
                raise ServiceError(400, "翻译接口仅接受文本消息")
            normalized.append({"role": message["role"], "content": content})
        messages = normalized
        source = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    else:
        source = _string(body, "source")
        lang = _string(body, "source_language", "auto")
        name = config.LANGUAGES.get(lang, lang)
        instruction = "将原文译成简体中文" if lang == "auto" else f"将{name}译成简体中文"
        instruction += "，只输出当前语句的译文。保留数字、否定、疑问和残句，不补写。"
        if topic:
            instruction += f"场景为{topic}。"
        context = _string(body, "context")[-512:]
        user = ("前文仅供理解：\n" + context + "\n\n" if context else "") + "当前语句：\n" + source
        messages = [{"role": "system", "content": instruction}, {"role": "user", "content": user}]
    if not source.strip():
        raise ServiceError(400, "待译原文为空")
    names = body.get("asr_names", []) if path == "/v1/captions" else []
    if not isinstance(names, list) or len(names) > 12 or any(not isinstance(name, str) for name in names):
        raise ServiceError(400, "asr_names 应为最多 12 个原文热词")
    hints = glossary.translation_hints(source, topic, names)
    if hints:
        note = "本句术语采用以下译法，近音须结合语境。\n" + hints
        if messages[0]["role"] == "system":
            messages[0]["content"] += "\n" + note
        else:
            messages.insert(0, {"role": "system", "content": note})
    limit = body.get("max_completion_tokens", body.get("max_tokens", 256 if path == "/v1/captions" else 512))
    if type(limit) is not int or limit < 1 or not isinstance(body.get("stream", False), bool):
        raise ServiceError(400, "max_tokens 应为正整数，stream 应为布尔值")
    payload = {k: body[k] for k in ("temperature", "top_p", "seed", "stop", "stream_options", "response_format",
                                    "frequency_penalty", "presence_penalty") if k in body}
    payload.setdefault("temperature", 0)
    payload.update(model=cfg["llm_model"], messages=messages, stream=body.get("stream", False),
                   max_tokens=min(limit, 512), chat_template_kwargs={"enable_thinking": False})
    return payload


class Gateway:
    """One inference slot. Admission, leases and model unloading use the same condition."""
    def __init__(self, cfg, runtime):
        self.cfg, self.runtime = cfg, runtime
        self.cv = threading.Condition()
        self.captions, self.bulk = deque(), deque()
        self.sessions = {}
        self.active = None
        self.loaded = self.stopping = False
        self.last_used = time.monotonic()
        self.gpu = FileLock(cfg["gpu_lock"], timeout=0, thread_local=False) if cfg.get("gpu_lock") else None
        self.glossary = Glossary("", phonetic=False)
        self.revision = object()

    def _reserve_gpu(self):
        if self.gpu and not self.gpu.is_locked:
            try:
                self.gpu.acquire()
            except Timeout:
                raise ServiceError(409, "GPU 正由其他转写任务使用") from None

    def _release_gpu(self):
        if self.gpu and self.gpu.is_locked and not (self.sessions or self.loaded or self.active or self.runtime.owned):
            self.gpu.release()

    def _prune(self):
        now = time.monotonic()
        expired = [s for s, seen in self.sessions.items() if now - seen > 90]
        for session in expired:
            del self.sessions[session]
        if expired and not self.sessions:
            self.last_used = now
            self._release_gpu()

    def session(self, action, token):
        with self.cv:
            self._prune()
            if self.stopping:
                raise ServiceError(503, "网关正在关闭")
            if action == "open":
                self._reserve_gpu()
                token = secrets.token_urlsafe(24)
            elif not isinstance(token, str) or token not in self.sessions:
                raise ServiceError(409, "字幕会话已结束，请重新启动字幕")
            if action == "close":
                del self.sessions[token]
                self.last_used = time.monotonic()
                self._release_gpu()
            else:
                self.sessions[token] = time.monotonic()
            self.cv.notify_all()
            return {"session": token}

    def info(self):
        with self.cv:
            self._prune()
            return {"service": "audio-translation", "model": self.cfg["llm_model"], "loaded": self.loaded,
                    "active": self.active, "caption_sessions": len(self.sessions), "queued_captions": len(self.captions),
                    "queued_text": len(self.bulk), "stopping": self.stopping}

    @contextmanager
    def slot(self, caption, token=None, disconnected=lambda: False):
        ticket = object()
        queue = self.captions if caption else self.bulk
        with self.cv:
            if len(queue) >= (16 if caption else 32):
                raise ServiceError(429, "等待翻译的请求过多，请稍后重试")
            queue.append(ticket)
            try:
                while True:
                    if self.stopping:
                        raise ServiceError(503, "网关正在关闭")
                    if caption and (not isinstance(token, str) or token not in self.sessions):
                        raise ServiceError(409, "字幕会话已结束")
                    if disconnected():
                        raise ConnectionAbortedError("client disconnected")
                    if not self.active and queue[0] is ticket and (caption or not self.captions):
                        self._reserve_gpu()
                        self.active = "caption" if caption else "text"
                        break
                    self.cv.wait(.2)
            finally:
                queue.remove(ticket)
                self.cv.notify_all()
        try:
            yield
        finally:
            with self.cv:
                self.active = None
                self.last_used = time.monotonic()
                self._release_gpu()
                self.cv.notify_all()

    def prepare(self, path, body):
        revision = config.shared_terms_revision()
        if self.revision != revision:
            self.glossary = Glossary(config.read_shared_terms(self.cfg), phonetic=False)
            self.revision = revision
        payload = translation_request(path, body, self.cfg, self.glossary)
        if not self.loaded or not self.runtime.ready():
            self.runtime.start(log.info)
            with self.cv:
                self.loaded = True
        return payload

    def request_stop(self):
        with self.cv:
            self._prune()
            if self.sessions:
                raise ServiceError(409, "请先停止正在运行的字幕，再关闭翻译服务")
            self.stopping = True
            self.cv.notify_all()

    def unload(self):
        try:
            self.runtime.stop()
            with self.cv:
                self.loaded = False
        finally:
            with self.cv:
                self.active = None
                self.last_used = time.monotonic()
                self._release_gpu()
                self.cv.notify_all()

    def maintain(self):
        while True:
            with self.cv:
                self._prune()
                if self.stopping:
                    return
                idle = time.monotonic() - self.last_used
                if (not self.active and not self.sessions and not self.captions and not self.bulk
                        and (self.loaded or self.runtime.owned) and idle >= self.cfg["gateway_idle_seconds"]):
                    self.active = "unload"
                else:
                    self.cv.wait(1)
                    continue
            try:
                self.unload()
            except Exception:
                log.exception("Idle unload failed")


class Server(ThreadingHTTPServer):
    daemon_threads = False  # finish active inference before releasing the mode lock

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.sent = False

    def _local(self):
        host = urlsplit("//" + self.headers.get("Host", "")).hostname
        origin = self.headers.get("Origin")
        if host not in LOCAL or (origin and urlsplit(origin).hostname not in LOCAL):
            raise ServiceError(403, "仅允许本机访问")

    def _headers(self, code, mime, size=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname in LOCAL:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if size is not None:
            self.send_header("Content-Length", str(size))
        if code == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.sent = True

    def _json(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self._headers(code, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _disconnected(self):
        return bool(select.select([self.connection], [], [], 0)[0]) and not self.connection.recv(1, socket.MSG_PEEK)

    def _translate(self, state, path, body):
        with state.slot(path == "/v1/captions", body.get("session"), self._disconnected):
            payload = state.prepare(path, body)
            if self._disconnected():
                return
            request = urllib.request.Request(state.runtime.url + "/v1/chat/completions",
                                             data=json.dumps(payload, ensure_ascii=False).encode(),
                                             headers={"Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=180) as upstream:
                if payload["stream"]:
                    self._headers(200, "text/event-stream; charset=utf-8")
                    for line in upstream:
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    self._json(200, json.load(upstream))

    def _handle(self):
        state = self.server.state
        try:
            self._local()
            path = urlsplit(self.path).path.rstrip("/")
            if self.command == "OPTIONS":
                self._headers(204, "text/plain", 0)
            elif self.command == "GET" and path == "/health":
                self._json(200, state.info())
            elif self.command == "GET" and path == "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": state.cfg["llm_model"], "object": "model", "owned_by": "local"}]})
            elif self.command == "POST":
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get_content_type() != "application/json":
                    raise ServiceError(415, "需要 application/json")
                if not 0 < length <= 131072:
                    raise ServiceError(413, "请求体须在 1 到 131072 字节之间")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ServiceError(400, "需要 JSON 对象")
                if path in ("/sessions/open", "/sessions/heartbeat", "/sessions/close"):
                    self._json(200, state.session(path.rsplit("/", 1)[1], body.get("session")))
                elif path == "/admin/stop":
                    state.request_stop()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    self._json(200, {"stopping": True})
                elif path in ("/v1/captions", "/v1/translate", "/v1/chat/completions"):
                    self._translate(state, path, body)
                else:
                    raise ServiceError(404, "未知接口")
            else:
                raise ServiceError(404, "未知接口")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            code = exc.code if isinstance(exc, (ServiceError, urllib.error.HTTPError)) else 400 if isinstance(exc, ValueError) else 502
            if not self.sent:
                self._json(code, {"error": {"message": str(exc)}})
            elif not isinstance(exc, OSError):
                log.warning("Interrupted upstream stream: %s", exc)

    do_POST = do_GET = do_OPTIONS = _handle


def mode_lock():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return FileLock(str(config.DATA_DIR / "translation-mode.lock"), timeout=0)


def serve(cfg):
    with mode_lock():
        if (config.DATA_DIR / "model-test.active").exists():
            raise RuntimeError("本地大模型测试尚未结束，请先从测试入口停止模型")
        runtime = ModelRuntime(cfg, log.info)
        if cfg.get("llm_controller") and runtime.ready():
            raise RuntimeError("检测到独立运行的 Qwen，请先从测试入口停止模型")
        config.save_shared_terms(cfg.get("terms", ""), initialize=True)
        state = Gateway(cfg, runtime)
        with Server(("127.0.0.1", int(cfg["gateway_port"])), state) as server:
            monitor = threading.Thread(target=state.maintain, name="model-idle")
            monitor.start()
            try:
                server.serve_forever(poll_interval=.1)
            finally:
                with state.cv:
                    state.stopping = True
                    state.cv.notify_all()
                server.server_close()
                monitor.join()
                state.unload()


def test_control(cfg, action):
    marker = config.DATA_DIR / "model-test.active"
    with mode_lock():
        if action == "test-check":
            if not marker.exists():
                raise RuntimeError("请先关闭翻译服务并从本地 AI 测试入口启动模型")
            return
        runtime = ModelRuntime(cfg, log.info)
        if not cfg.get("llm_controller"):
            raise RuntimeError("本地模型测试需要配置 llm_controller")
        if action == "test-start":
            marker.touch()
            if not runtime.ready():
                runtime.control("start", 300)
            if not runtime.ready():
                raise RuntimeError("测试模型未就绪，请用测试入口停止后重试")
        else:
            # Also allows explicit recovery of a previously started standalone model.
            if marker.exists() or runtime.ready():
                runtime.control("stop", 120)
            if runtime.ready():
                raise RuntimeError("测试模型仍在运行")
            marker.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local audio/text translation gateway")
    parser.add_argument("action", choices=["serve", "start", "stop", "status", "test-start", "test-stop", "test-check"])
    action = parser.parse_args(argv).action
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if action == "serve":
            serve(cfg)
        elif action == "start":
            ensure_gateway(cfg)
            print(gateway_url(cfg))
        elif action == "status":
            print(json.dumps(status(cfg) or {"service": "stopped", "model_test": (config.DATA_DIR / "model-test.active").exists()}, ensure_ascii=False))
        elif action == "stop":
            with FileLock(str(config.DATA_DIR / "gateway-launch.lock"), timeout=25):
                info = status(cfg)
                if info and not info["stopping"]:
                    request_json(cfg, "/admin/stop", {})
                with mode_lock().acquire(timeout=360):
                    testing = (config.DATA_DIR / "model-test.active").exists()
                    if cfg.get("llm_controller") and not testing and ModelRuntime(cfg, log.info).ready():
                        raise RuntimeError("网关已退出，模型仍在运行。请用 test-stop 结束本机模型。")
            print("翻译网关已关闭")
        else:
            test_control(cfg, action)
    except Timeout:
        print("翻译服务或测试操作正在运行。请先停止字幕并关闭翻译网关，再启动本地 AI 测试。", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
