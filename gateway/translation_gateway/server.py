"""Translation gateway: one local model shared by audio captions and text clients on this PC and the LAN."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import re
import secrets
import select
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import parse_http_list

from filelock import FileLock, Timeout

from . import config
from .access import (LAN_NETWORKS, DeviceRegistry, add_device, list_devices, parse_networks, peer_address,
                     remote_allowed, revoke_device)
from .cache import TranslationCache
from .client import SERVICE, ensure_gateway, gateway_url, request_json, status
from .runtime import ModelRuntime
from .terms import Glossary

log = logging.getLogger(__name__)
LOCAL = {"localhost", "127.0.0.1", "::1"}
EXTENSION_SCHEMES = {"chrome-extension", "moz-extension", "safari-web-extension", "extension"}
LOCAL_ONLY = {"/v1/captions", "/sessions/open", "/sessions/heartbeat", "/sessions/close", "/admin/stop", "/admin/terms"}
CACHEABLE = {"/v1/translate", "/v1/chat/completions"}
CAPTION_STATES = {"loading", "running", "paused", "stopping"}
BODY_LIMIT = 128 * 1024
TERMS_BODY_LIMIT = 8 * 1024 * 1024
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


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


def cache_key(payload):
    """Everything that shapes the output, i.e. the upstream request minus transport options."""
    stable = {k: v for k, v in payload.items() if k not in ("stream", "stream_options")}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def completion(meta, text, finish, usage=None):
    result = {"id": meta["id"], "object": "chat.completion", "created": meta["created"], "model": meta["model"],
              "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}]}
    if usage:
        result["usage"] = usage
    return result


def _percentile(values, q):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 3) if ordered else None


class Stats:
    """Per-device latency and cache counters. No request text is kept."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}

    def record(self, client, kind, outcome, *, first_token_s=None, queue_s=None, total_s=None):
        with self.lock:
            entry = self.clients.setdefault(client, {}).setdefault(kind, {
                "requests": 0, "cache_hits": 0, "errors": 0, "cancelled": 0,
                "first_token": deque(maxlen=200), "queue": deque(maxlen=200), "total": deque(maxlen=200)})
            entry["requests"] += 1
            entry["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if outcome == "hit":
                entry["cache_hits"] += 1
            elif outcome == "error":
                entry["errors"] += 1
            elif outcome == "cancelled":
                entry["cancelled"] += 1
            for name, value in (("first_token", first_token_s), ("queue", queue_s), ("total", total_s)):
                if value is not None and outcome in ("ok", "hit"):
                    entry[name].append(value)

    def snapshot(self):
        with self.lock:
            report = {}
            for client, kinds in self.clients.items():
                for kind, e in kinds.items():
                    report.setdefault(client, {})[kind] = {
                        "requests": e["requests"], "cache_hits": e["cache_hits"],
                        "cache_hit_rate": round(e["cache_hits"] / e["requests"], 3) if e["requests"] else 0,
                        "errors": e["errors"], "cancelled": e["cancelled"], "last_seen": e.get("last_seen"),
                        "first_token_p50_s": _percentile(e["first_token"], .5), "first_token_p95_s": _percentile(e["first_token"], .95),
                        "queue_p50_s": _percentile(e["queue"], .5), "total_p50_s": _percentile(e["total"], .5),
                        "total_p95_s": _percentile(e["total"], .95)}
            return report


class Gateway:
    """Admission, leases and model unloading share one condition.

    ``llm_parallel`` is the number of generations the inference server runs at once (1 for a
    single-slot server). With two or more, text requests use at most all but one, so a caption
    never waits for a long text generation.
    """
    def __init__(self, cfg, runtime):
        self.cfg, self.runtime = cfg, runtime
        self.cv = threading.Condition()
        self.captions, self.bulk = deque(), deque()
        self.sessions = {}
        self.parallel = max(1, int(cfg.get("llm_parallel", 1)))
        self.running = {"caption": 0, "text": 0}
        self.loaded = self.loading = self.unloading = self.stopping = False
        self.load_lock = threading.Lock()
        self.last_used = self.started = time.monotonic()
        self.gpu = FileLock(cfg["gpu_lock"], timeout=0, thread_local=False) if cfg.get("gpu_lock") else None
        self.glossary = Glossary("", phonetic=False)
        self.revision = object()
        self.terms_hash = ""
        self.glossary_lock = threading.Lock()
        self.networks = parse_networks(cfg.get("gateway_networks"))
        self.devices = DeviceRegistry()
        self.cache = (TranslationCache(config.cache_dir(cfg) / "translation-cache.sqlite3", cfg.get("gateway_cache_entries", 20000))
                      if cfg.get("gateway_cache", True) else None)
        self.stats = Stats()

    def close(self):
        if self.cache:
            self.cache.close()

    def _busy(self):
        return sum(self.running.values())

    def _reserve_gpu(self):
        if self.gpu and not self.gpu.is_locked:
            try:
                self.gpu.acquire()
            except Timeout:
                raise ServiceError(409, "GPU 正由其他转写任务使用") from None

    def _release_gpu(self):
        if self.gpu and self.gpu.is_locked and not (self.sessions or self.loaded or self.loading or self._busy() or self.runtime.owned):
            self.gpu.release()

    def _prune(self):
        now = time.monotonic()
        expired = [s for s, entry in self.sessions.items() if now - entry["seen"] > 90]
        for session in expired:
            del self.sessions[session]
        if expired and not self.sessions:
            self.last_used = now
            self._release_gpu()

    def session(self, action, token, caption_state=None):
        """Caption sessions; open and heartbeat may report what the caption app is doing."""
        if caption_state is not None and (not isinstance(caption_state, str) or caption_state not in CAPTION_STATES):
            raise ServiceError(400, "state 应为 " + "、".join(sorted(CAPTION_STATES)))
        revision = self.terms()[1]
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
                previous = self.sessions.get(token, {}).get("state", "loading")
                self.sessions[token] = {"seen": time.monotonic(), "state": caption_state or previous}
            self.cv.notify_all()
            return {"session": token, "terms_revision": revision}

    def _caption_states(self):
        counts = {}
        for entry in self.sessions.values():
            counts[entry["state"]] = counts.get(entry["state"], 0) + 1
        return counts

    def _active(self):
        if self.unloading:
            return "unload"
        return "caption" if self.running["caption"] else "text" if self.running["text"] else None

    def _model_state(self):
        return "unloading" if self.unloading else "loading" if self.loading else "ready" if self.loaded else "unloaded"

    def info(self):
        with self.cv:
            self._prune()
            return {"service": SERVICE, "model": self.cfg["llm_model"], "loaded": self.loaded,
                    "active": self._active(), "caption_sessions": len(self.sessions), "queued_captions": len(self.captions),
                    "queued_text": len(self.bulk), "stopping": self.stopping}

    def status_report(self):
        with self.cv:
            self._prune()
            report = {"service": SERVICE,
                      "model": {"id": self.cfg["llm_model"], "label": self.cfg.get("llm_label") or self.cfg["llm_model"],
                                "state": self._model_state(), "parallel": self.parallel,
                                "idle_unload_s": self.cfg["gateway_idle_seconds"]},
                      "running": dict(self.running), "queued": {"captions": len(self.captions), "text": len(self.bulk)},
                      "caption_sessions": len(self.sessions), "caption_states": self._caption_states(),
                      "stopping": self.stopping, "uptime_s": round(time.monotonic() - self.started)}
        report["network"] = {"lan": bool(self.cfg.get("gateway_lan")), "port": int(self.cfg["gateway_port"]),
                             "remote_networks": [str(n) for n in self.networks or LAN_NETWORKS], "devices": self.devices.count()}
        report["cache"] = {"enabled": self.cache is not None, "entries": self.cache.count() if self.cache else 0}
        glossary, revision = self.terms()
        report["terms"] = {"entries": len(glossary.entries), "revision": revision}
        report["clients"] = self.stats.snapshot()
        return report

    def _admissible(self, caption):
        if self.unloading or self._busy() >= self.parallel:
            return False
        return caption or (not self.captions and self.running["text"] < max(1, self.parallel - 1))

    @contextmanager
    def slot(self, caption, token=None, disconnected=lambda: False):
        ticket = object()
        kind = "caption" if caption else "text"
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
                    if queue[0] is ticket and self._admissible(caption):
                        self._reserve_gpu()
                        self.running[kind] += 1
                        break
                    self.cv.wait(.2)
            finally:
                queue.remove(ticket)
                self.cv.notify_all()
        try:
            yield
        finally:
            with self.cv:
                self.running[kind] -= 1
                self.last_used = time.monotonic()
                self._release_gpu()
                self.cv.notify_all()

    def terms(self, *, reload=False):
        """The shared glossary and its content hash, reread after every change of the file."""
        revision = config.terms_revision()
        with self.glossary_lock:
            if reload or self.revision != revision:
                raw = config.read_terms()
                self.glossary = Glossary(raw, phonetic=False)
                self.terms_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
                self.revision = revision
            return self.glossary, self.terms_hash

    def save_terms(self, raw):
        if not isinstance(raw, str):
            raise ServiceError(400, "terms 应为文本")
        config.save_terms(raw)
        glossary, revision = self.terms(reload=True)
        return {"revision": revision, "entries": len(glossary.entries)}

    def build(self, path, body):
        return translation_request(path, body, self.cfg, self.terms()[0])

    def ensure_loaded(self):
        with self.load_lock:
            if self.loaded and self.runtime.ready():
                return
            with self.cv:
                self.loading = True
            try:
                self.runtime.start(log.info)
            finally:
                with self.cv:
                    self.loading = False
            with self.cv:
                self.loaded = True

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
                self.unloading = False
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
                if (not self._busy() and not self.loading and not self.sessions and not self.captions and not self.bulk
                        and (self.loaded or self.runtime.owned) and idle >= self.cfg["gateway_idle_seconds"]):
                    self.unloading = True
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
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            # "::" also accepts IPv4 clients; their addresses arrive as ::ffff:a.b.c.d.
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _abort(sock):
    """Close a socket that another thread is blocked reading from.

    Windows only wakes that read when the handle is closed, and a plain close() waits for the
    HTTP response's file object, so the handle is detached and closed directly.
    """
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        handle = sock.detach()
        if handle != -1:
            socket.close(handle)
    except OSError:
        pass


def _event(line):
    """Parse one SSE line from the model: a dict, "done", or None for anything else."""
    if not line.startswith(b"data:"):
        return None
    data = line[5:].strip()
    if data == b"[DONE]":
        return "done"
    try:
        event = json.loads(data)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.sent = False

    def _peer(self):
        return peer_address(self.client_address[0])

    def _bearer(self):
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        return value.strip() if scheme.lower() == "bearer" else ""

    def _origin_allowed(self, origin):
        """CORS: browser extensions and local pages on this PC; any origin on an allowed LAN peer,
        whose requests still need a device token."""
        peer = self._peer()
        if peer.is_loopback:
            parts = urlsplit(origin)
            return parts.scheme in EXTENSION_SCHEMES or (parts.scheme in ("http", "https") and parts.hostname in LOCAL)
        return remote_allowed(peer, self.server.state.networks)

    def _authorize(self, state, path):
        """Return the client name: a device, or "local" for this PC without a token."""
        peer = self._peer()
        device = state.devices.lookup(self._bearer())
        if peer.is_loopback:
            if not device:  # Host and Origin checks keep web pages (DNS rebinding) out
                origin = self.headers.get("Origin")
                if urlsplit("//" + self.headers.get("Host", "")).hostname not in LOCAL or (origin and not self._origin_allowed(origin)):
                    raise ServiceError(403, "仅允许本机访问")
            return device or "local"
        if not remote_allowed(peer, state.networks):
            raise ServiceError(403, "来源地址不在允许的局域网范围内")
        if not device:
            raise ServiceError(401, "需要有效的设备 token（Authorization: Bearer …）")
        if path in LOCAL_ONLY:
            raise ServiceError(403, "该接口仅限本机使用")
        return device

    def _headers(self, code, mime, size=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            requested = (h.strip() for h in self.headers.get("Access-Control-Request-Headers", "").split(","))
            allowed = ["Content-Type", "Authorization"] + [h for h in requested if HEADER_NAME.match(h)
                                                          and h.lower() not in ("content-type", "authorization")]
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", ", ".join(allowed))
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "X-Gateway-Cache, Retry-After")
            self.send_header("Access-Control-Max-Age", "600")
            if self.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if size is not None:
            self.send_header("Content-Length", str(size))
        if code == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.sent = True

    def _json(self, code, value, extra=None):
        data = json.dumps(value, ensure_ascii=False).encode()
        self._headers(code, "application/json; charset=utf-8", len(data), extra)
        self.wfile.write(data)

    def _disconnected(self):
        return bool(select.select([self.connection], [], [], 0)[0]) and not self.connection.recv(1, socket.MSG_PEEK)

    def _send_cached(self, payload, text):
        """Replay a cached translation like a fresh one. It used no model tokens."""
        meta = {"id": "chatcmpl-cache-" + secrets.token_hex(8), "created": int(time.time()), "model": payload["model"]}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not payload["stream"]:
            self._json(200, completion(meta, text, "stop", usage), {"X-Gateway-Cache": "hit"})
            return
        self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Gateway-Cache": "hit"})
        chunk = dict(meta, object="chat.completion.chunk")
        events = [dict(chunk, choices=[{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]),
                  dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])]
        options = payload.get("stream_options")
        if isinstance(options, dict) and options.get("include_usage"):
            events.append(dict(chunk, choices=[], usage=usage))
        for event in events:
            self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _generate(self, state, payload, marks):
        """Always stream from the model, so a client that goes away also stops its generation.
        A watcher notices a departed client even while the model has not answered yet.

        Returns (text, finish_reason, complete, reply), or None after the client disconnected. The end
        of the answer (the JSON body, or a stream's [DONE]) is left to ``reply`` so that the caller
        can update the cache and statistics before the client sees the answer complete."""
        relay = payload["stream"]
        target = urlsplit(state.runtime.url)
        connection = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = connection(target.hostname, target.port, timeout=180)
        finished, gone = threading.Event(), threading.Event()

        def watch():
            while not finished.wait(.2):
                try:
                    left = self._disconnected()
                except OSError:
                    left = True
                if left:
                    gone.set()
                    _abort(upstream_socket)
                    return

        parts, finish, done, usage, seen = [], None, False, None, set()
        meta = {"id": "chatcmpl-" + secrets.token_hex(8), "created": int(time.time()), "model": payload["model"]}
        upstream = None
        try:
            conn.request("POST", target.path.rstrip("/") + "/v1/chat/completions",
                         json.dumps(dict(payload, stream=True), ensure_ascii=False).encode(), {"Content-Type": "application/json"})
            # getresponse() can transfer a closing connection to the response and clear conn.sock.
            upstream_socket = conn.sock
            threading.Thread(target=watch, name="client-watch", daemon=True).start()
            upstream = conn.getresponse()
            if upstream.status != 200:
                detail = upstream.read(600).decode("utf-8", "replace")
                raise ServiceError(upstream.status if 400 <= upstream.status < 600 else 502,
                                   f"本地翻译服务 HTTP {upstream.status}：{detail}")
            if relay:
                self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Gateway-Cache": "miss"})
            for line in upstream:
                event = _event(line)
                if event == "done":
                    done = True
                    break
                if relay:
                    self.wfile.write(line)
                    self.wfile.flush()
                elif self._disconnected():
                    return None
                if event is None:
                    continue
                if event.get("error"):
                    if relay:
                        return "".join(parts), "error", False, lambda: None
                    raise ServiceError(502, f"翻译流中断：{event['error']}")
                for key in ("id", "created", "model"):
                    if event.get(key) and key not in seen:
                        meta[key] = event[key]
                        seen.add(key)
                usage = event.get("usage") or usage
                for choice in event.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        marks.setdefault("first", time.monotonic())
                        parts.append(delta)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except (OSError, http.client.HTTPException):
            if gone.is_set():
                return None
            raise
        finally:
            finished.set()
            if upstream is not None:
                upstream.close()
            conn.close()
        if gone.is_set():
            return None
        text = "".join(parts)
        complete = done or finish is not None
        if relay:
            def reply():
                if done:  # the end marker, with the blank line on which SSE dispatches it
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
        else:
            if not complete:
                raise ServiceError(502, "本地翻译服务在译文完成前断开了连接")

            def reply():
                self._json(200, completion(meta, text, finish or "stop", usage), {"X-Gateway-Cache": "miss"})
        return text, finish, complete, reply

    def _translate(self, state, client, path, body):
        caption = path == "/v1/captions"
        kind = "caption" if caption else "text"
        start, marks, outcome, recorded = time.monotonic(), {}, "error", False

        def record():
            nonlocal recorded
            if recorded:
                return
            recorded = True
            end = time.monotonic()
            state.stats.record(client, kind, outcome, total_s=end - start,
                               first_token_s=marks["first"] - start if "first" in marks else (end - start if outcome == "hit" else None),
                               queue_s=marks["admitted"] - start if "admitted" in marks else None)

        try:
            payload = state.build(path, body)
            key = cache_key(payload) if state.cache and path in CACHEABLE and payload.get("temperature") == 0 else None
            cached = None
            if key:
                try:
                    cached = state.cache.get(key)
                except (sqlite3.Error, OSError) as exc:
                    log.warning("Cache lookup failed: %s", type(exc).__name__)
            if cached is not None:
                outcome = "hit"
                record()  # bookkeeping first: a client holding the answer also finds it counted
                self._send_cached(payload, cached)
                return
            with state.slot(caption, body.get("session"), self._disconnected):
                marks["admitted"] = time.monotonic()
                state.ensure_loaded()
                if self._disconnected():
                    outcome = "cancelled"
                    return
                result = self._generate(state, payload, marks)
            if result is None:
                outcome = "cancelled"
                return
            text, finish, complete, reply = result
            outcome = "ok" if complete and finish in (None, "stop") else "error"
            # "Cache-Control: no-store" keeps this translation off the disk; cached answers may still serve it.
            directives = parse_http_list(",".join(self.headers.get_all("Cache-Control", [])))
            store = all(item.partition("=")[0].strip().lower() != "no-store" for item in directives)
            if key and store and outcome == "ok" and text.strip():
                try:
                    state.cache.put(key, text, payload["model"])
                except (sqlite3.Error, OSError) as exc:
                    log.warning("Cache write failed: %s", type(exc).__name__)
            record()
            reply()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if not recorded:
                outcome = "cancelled"
            raise
        finally:
            record()

    def _handle(self):
        state = self.server.state
        try:
            path = urlsplit(self.path).path.rstrip("/")
            if self.command == "OPTIONS":
                origin = self.headers.get("Origin")
                if not origin or not self._origin_allowed(origin):
                    raise ServiceError(403, "不允许的跨域来源")
                self._headers(204, "text/plain", 0)
                return
            client = self._authorize(state, path)
            if self.command == "GET" and path == "/health":
                self._json(200, state.info())
            elif self.command == "GET" and path == "/status":
                self._json(200, state.status_report())
            elif self.command == "GET" and path == "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": state.cfg["llm_model"], "object": "model", "owned_by": "local"}]})
            elif self.command == "GET" and path == "/admin/terms":
                glossary, revision = state.terms()
                self._json(200, {"terms": glossary.raw, "revision": revision, "entries": len(glossary.entries)})
            elif self.command == "POST":
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get_content_type() != "application/json":
                    raise ServiceError(415, "需要 application/json")
                limit = TERMS_BODY_LIMIT if path == "/admin/terms" else BODY_LIMIT  # a whole glossary is larger than a request
                if not 0 < length <= limit:
                    raise ServiceError(413, f"请求体须在 1 到 {limit} 字节之间")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ServiceError(400, "需要 JSON 对象")
                if path in ("/sessions/open", "/sessions/heartbeat", "/sessions/close"):
                    self._json(200, state.session(path.rsplit("/", 1)[1], body.get("session"), body.get("state")))
                elif path == "/admin/stop":
                    state.request_stop()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    self._json(200, {"stopping": True})
                elif path == "/admin/terms":
                    self._json(200, state.save_terms(body.get("terms")))
                elif path in ("/v1/captions", "/v1/translate", "/v1/chat/completions"):
                    self._translate(state, client, path, body)
                else:
                    raise ServiceError(404, "未知接口")
            else:
                raise ServiceError(404, "未知接口")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            code = exc.code if isinstance(exc, ServiceError) else 400 if isinstance(exc, ValueError) else 502
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
        state = Gateway(cfg, runtime)
        host = ("::" if socket.has_ipv6 else "0.0.0.0") if cfg.get("gateway_lan") else "127.0.0.1"
        if cfg.get("gateway_lan") and not state.devices.count():
            log.warning("已开放局域网访问，但还没有设备 token。运行 translation-gateway.cmd token-add <设备名> 添加。")
        try:
            with Server((host, int(cfg["gateway_port"])), state) as server:
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
        finally:
            state.close()


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


def device_control(cfg, action, name):
    if action == "token-list":
        devices = list_devices()
        for device in devices:
            print(f"{device['name']}\t{device['created']}")
        if not devices:
            print("还没有设备 token")
        return
    if not name:
        raise RuntimeError(f"用法：translation-gateway.cmd {action} <设备名>")
    if action == "token-revoke":
        if not revoke_device(name):
            raise RuntimeError(f"没有名为 {name} 的设备")
        print(f"已吊销设备 {name}，运行中的网关立即生效")
        return
    token = add_device(name)
    print(f"设备 {name} 的 token（只显示这一次，请立即填进该设备的客户端）：")
    print(token)
    print(f"客户端设置：Base URL http://<这台电脑的局域网 IP>:{int(cfg['gateway_port'])}/v1 ，API Key 填上面的 token。")
    if not cfg.get("gateway_lan"):
        print(f"提示：{config.CONFIG_PATH} 中 gateway_lan 仍为 false，改为 true 并重启网关后其他设备才能连接。")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="translation-gateway", description="Local translation gateway for audio and text clients")
    parser.add_argument("action", choices=["serve", "start", "stop", "status", "test-start", "test-stop", "test-check",
                                           "token-add", "token-list", "token-revoke", "cache-clear"])
    parser.add_argument("name", nargs="?", help="设备名（token-add / token-revoke）")
    args = parser.parse_args(argv)
    action = args.action
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
        elif action.startswith("token-"):
            device_control(cfg, action, args.name)
        elif action == "cache-clear":
            cache = TranslationCache(config.cache_dir(cfg) / "translation-cache.sqlite3")
            try:
                print(f"已清除 {cache.clear()} 条翻译缓存")
            finally:
                cache.close()
        else:
            test_control(cfg, action)
    except Timeout:
        print("翻译服务或测试操作正在运行。请先停止字幕并关闭翻译网关，再启动本地 AI 测试。", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
