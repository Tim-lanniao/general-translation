"""Client helpers for programs on this PC: find, start and call the gateway, read its streams.

This module and ``config``/``terms`` are the gateway's Python interface for other
components; everything else in the package is internal to the gateway process.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import CancelledError
from pathlib import Path

from filelock import FileLock

from . import config

SERVICE = "translation-gateway"
_LABELS = ("当前语句", "译文", "翻译", "中文")
_LEAK = re.compile(r"^(?:当前语句|译文|翻译|中文)\s*[:：]\s*")
_DETACHED = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def gateway_url(cfg):
    return cfg.get("gateway_url") or f"http://127.0.0.1:{int(cfg.get('gateway_port', 18083))}"


def request_json(cfg, path, body=None, timeout=5):
    """GET without a body, POST with one."""
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
    if result.get("service") != SERVICE:
        raise RuntimeError("翻译网关端口已被其他服务占用")
    return result


def _python():
    """The gateway needs only the standard library and filelock, so any of the project's
    interpreters can run it; TRANSLATION_GATEWAY_PYTHON selects a dedicated one."""
    chosen = os.environ.get("TRANSLATION_GATEWAY_PYTHON")
    return chosen if chosen and Path(chosen).is_file() else sys.executable


def ensure_gateway(cfg):
    if cfg.get("gateway_url"):
        # A caller managing its own endpoint must never start another gateway on the local config port.
        info = status(cfg)
        if not info or info["stopping"]:
            raise RuntimeError("指定的翻译网关未就绪，请在翻译入口检查网关状态")
        return
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
            process = subprocess.Popen([_python(), "-B", "-X", "utf8", "-m", "translation_gateway", "serve"],
                                       cwd=config.COMPONENT, env={**os.environ, "TRANSLATION_DATA": str(config.ROOT)},
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=_DETACHED)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if status(cfg):
                return
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise RuntimeError(f"翻译网关启动失败，详见 {log_path}")


def stream_chat(url: str, body: dict, timeout: float = 30, *, on_partial=None, cancelled=None) -> tuple[str, float, str | None]:
    """Read an OpenAI-style SSE response; ``on_partial`` receives the cleaned text so far."""
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
