"""Streaming caption client and local gateway control. Model ownership stays in the gateway."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import CancelledError

from filelock import FileLock

from . import config

_LABELS = ("当前语句", "译文", "翻译", "中文")
_LEAK = re.compile(r"^(?:当前语句|译文|翻译|中文)\s*[:：]\s*")


def gateway_url(cfg):
    return f"http://127.0.0.1:{int(cfg.get('gateway_port', 18083))}"


def request_json(cfg, path, body=None, timeout=5):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(gateway_url(cfg) + path, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"翻译网关 HTTP {exc.code} · {detail}") from exc


def status(cfg):
    try:
        result = request_json(cfg, "/health", timeout=1)
    except (OSError, ValueError):
        return None
    if result.get("service") != "audio-translation":
        raise RuntimeError("翻译网关端口已被其他服务占用")
    return result


def ensure_gateway(cfg):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with FileLock(str(config.DATA_DIR / "gateway-launch.lock"), timeout=20):
        info = status(cfg)
        if info:
            if info["stopping"]:
                raise RuntimeError("翻译网关正在关闭，请稍后重试")
            return
        if (config.DATA_DIR / "model-test.active").exists():
            raise RuntimeError("本地大模型测试正在使用模型，请先从测试入口停止模型")
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = config.LOG_DIR / "translation-gateway.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "teams_caption.gateway", "serve"],
                                       cwd=config.REPO, env={**os.environ, "TEAMS_CAPTION_DATA": str(config.DATA_DIR)},
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if status(cfg):
                return
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise RuntimeError(f"翻译网关启动失败，详见 {log_path}")


def _stream_chat(url: str, body: dict, timeout: float = 30, *, on_partial=None, cancelled=None) -> tuple[str, float, str | None]:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.perf_counter()
    raw, shown, finish = "", "", None
    done = False
    if cancelled and cancelled():
        raise CancelledError()
    with opener.open(req, timeout=timeout) as resp:
        for line in resp:
            if cancelled and cancelled():
                raise CancelledError()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                done = True
                break
            event = json.loads(payload)
            if event.get("error"):
                raise RuntimeError(f"翻译流中断：{event['error']}")
            for c in event.get("choices", []):
                delta = c.get("delta", {}).get("content")
                if c.get("finish_reason"):
                    finish = c["finish_reason"]
                if not delta:
                    continue
                raw += delta
                if on_partial:
                    prefix = raw.strip()
                    if any(label.startswith(prefix) for label in _LABELS):
                        continue  # a model may send "译", "文", "：" separately
                    preview = _LEAK.sub("", prefix).strip()
                    if preview and preview != shown:
                        on_partial(preview)
                        shown = preview
    if not (done or finish):
        raise RuntimeError("本地翻译服务在译文完成前断开了连接")
    if finish and finish != "stop":
        raise RuntimeError(f"本地翻译服务提前结束了译文（{finish}）")
    text = _LEAK.sub("", raw.strip()).strip()
    if not text:
        raise RuntimeError("本地翻译服务返回了空译文")
    if on_partial and text != shown:
        on_partial(text)
    return text, time.perf_counter() - start, finish


class GatewayTranslator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.label = cfg.get("llm_label") or cfg["llm_model"]
        self.session = None
        self.closed = threading.Event()
        self.thread = None

    def start(self, progress):
        progress("连接本地翻译网关…")
        ensure_gateway(self.cfg)
        self.session = request_json(self.cfg, "/sessions/open", {})["session"]
        self.thread = threading.Thread(target=self._heartbeat, name="caption-session", daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.closed.wait(10):
            try:
                request_json(self.cfg, "/sessions/heartbeat", {"session": self.session})
            except (OSError, RuntimeError):
                if self.closed.wait(1):
                    return

    def stop(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.session:
            request_json(self.cfg, "/sessions/close", {"session": self.session})
            self.session = None

    def translate(self, source, context, topic, context_chars=160, *, source_language=None, asr_names=(), on_partial=None, cancelled=None):
        body = {"source": source, "context": context[-context_chars:] if context_chars > 0 else "",
                "source_language": source_language or self.cfg.get("asr_language", "de"), "topic": topic,
                "session": self.session, "stream": True, "asr_names": list(asr_names)}
        try:
            text, seconds, finish = _stream_chat(gateway_url(self.cfg) + "/v1/captions", body, timeout=360,
                                                 on_partial=on_partial, cancelled=cancelled)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"翻译网关 HTTP {exc.code} · {exc.read().decode('utf-8', 'replace')}") from exc
        return {"translation": text, "mt_s": seconds, "finish_reason": finish}
