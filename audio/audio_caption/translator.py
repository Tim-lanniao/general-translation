"""Caption client of the translation gateway: a session with priority, streamed sentences."""
from __future__ import annotations

import os
import threading
import urllib.error

from translation_gateway import client
from translation_gateway import config as gateway_config


def gateway_settings():
    cfg = gateway_config.load()
    url = os.environ.get("TRANSLATION_GATEWAY_URL")
    if url:
        cfg = dict(cfg, gateway_url=url.rstrip("/"))
    return cfg


class GatewayTranslator:
    def __init__(self, cfg, gateway_cfg=None):
        self.cfg = cfg
        self.gateway_cfg = gateway_cfg if gateway_cfg is not None else gateway_settings()
        self.label = self.gateway_cfg.get("llm_label") or self.gateway_cfg["llm_model"]
        self.session = None
        self.state = "loading"      # what this session tells the gateway the caption app is doing
        self.terms_revision = None  # the gateway's glossary version, part of the caption cache key
        self.closed = threading.Event()
        self.thread = None

    def _call(self, path, body, timeout=5):
        result = client.request_json(self.gateway_cfg, path, body, timeout=timeout)
        self.terms_revision = result.get("terms_revision", self.terms_revision)
        return result

    def start(self, progress):
        progress("连接本地翻译网关…")
        client.ensure_gateway(self.gateway_cfg)
        self.session = self._call("/sessions/open", {"state": self.state})["session"]
        self.thread = threading.Thread(target=self._heartbeat, name="caption-session", daemon=True)
        self.thread.start()

    def report(self, state):
        """Tell the gateway at once that the caption app is loading, running, paused or stopping."""
        self.state = state
        if self.session and not self.closed.is_set():
            try:
                self._call("/sessions/heartbeat", {"session": self.session, "state": state}, timeout=2)
            except (OSError, RuntimeError):
                pass  # the next heartbeat carries it

    def _heartbeat(self):
        while not self.closed.wait(10):
            try:
                self._call("/sessions/heartbeat", {"session": self.session, "state": self.state})
            except (OSError, RuntimeError):
                if self.closed.wait(1):
                    return

    def stop(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.session:
            client.request_json(self.gateway_cfg, "/sessions/close", {"session": self.session})
            self.session = None

    def translate(self, source, context, topic, context_chars=160, *, source_language=None, asr_names=(), on_partial=None, cancelled=None):
        body = {"source": source, "context": context[-context_chars:] if context_chars > 0 else "",
                "source_language": source_language or self.cfg.get("asr_language", "de"), "topic": topic,
                "session": self.session, "stream": True, "asr_names": list(asr_names)}
        try:
            text, seconds, finish = client.stream_chat(client.gateway_url(self.gateway_cfg) + "/v1/captions", body, timeout=360,
                                                       on_partial=on_partial, cancelled=cancelled)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"翻译网关 HTTP {exc.code} · {exc.read().decode('utf-8', 'replace')}") from exc
        return {"translation": text, "mt_s": seconds, "finish_reason": finish}


def read_shared_terms():
    """The gateway's glossary, starting the gateway if needed (it does not load the model)."""
    cfg = gateway_settings()
    client.ensure_gateway(cfg)
    return client.request_json(cfg, "/admin/terms")


def save_shared_terms(raw: str):
    cfg = gateway_settings()
    client.ensure_gateway(cfg)
    return client.request_json(cfg, "/admin/terms", {"terms": raw})
