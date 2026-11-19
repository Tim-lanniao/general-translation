"""Translation back ends (local only).

qwen   – any local OpenAI-compatible chat server (``llm_url`` / ``llm_model``). The author
         uses Qwen3.8-Flash-Next IQ3_S with thinking disabled. If ``llm_controller`` is set
         (a .ps1 taking start | stop | status) the server is started on demand and stopped
         again afterwards; otherwise it must already be running.
hymt7b – Hy-MT2-7B Q6_K served by llama.cpp ``llama-server`` that this program starts
         itself (faster and ~11 GB VRAM, but weaker on word sense).
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.request
from concurrent.futures import CancelledError
from pathlib import Path

from . import config

NO_WINDOW = subprocess.CREATE_NO_WINDOW
_LEAK = re.compile(r"^(?:当前语句|译文|翻译|中文)\s*[:：]\s*")


def _http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _stream_chat(url: str, body: dict, timeout: float = 30, *, on_partial=None, cancelled=None) -> tuple[str, float, str | None]:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.perf_counter()
    next_preview = start
    parts, finish = [], None
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
            for c in event.get("choices", []):
                delta = c.get("delta", {}).get("content")
                if c.get("finish_reason"):
                    finish = c["finish_reason"]
                if not delta:
                    continue
                parts.append(delta)
                if on_partial and not finish and time.perf_counter() >= next_preview:
                    preview = _LEAK.sub("", "".join(parts).strip()).strip()
                    if len(preview) >= 4 and preview != "当前语句":
                        on_partial(preview)
                        next_preview = time.perf_counter() + 0.18
    if not (done or finish):
        raise RuntimeError("本地翻译服务在译文完成前断开了连接")
    text = _LEAK.sub("", "".join(parts).strip()).strip()
    if not text:
        raise RuntimeError("本地翻译服务返回了空译文")
    return text, time.perf_counter() - start, finish


class LLMTranslator:
    name = "qwen"

    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.owned = False
        self.url = cfg["llm_url"].rstrip("/")
        self.label = cfg.get("llm_label") or cfg["llm_model"]

    def ready(self) -> bool:
        return _http_ok(self.url + "/health") or _http_ok(self.url + "/v1/models")

    def _control(self, action: str, timeout: float) -> str:
        script = Path(self.cfg.get("llm_controller") or "")
        if not script.is_file():
            raise FileNotFoundError(f"找不到翻译服务启动脚本：{script}")
        out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), action],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        text = (out.stdout or "") + (out.stderr or "")
        self.log(f"llm controller {action} -> exit {out.returncode}: {text.strip()[-600:]}")
        if out.returncode != 0:
            raise RuntimeError(f"翻译服务 {action} 失败（exit {out.returncode}）：{text.strip()[-300:]}")
        return text

    def start(self, progress) -> None:
        if self.ready():
            self.owned = False
            progress(f"翻译模型 {self.label} 已在运行，直接使用")
            return
        if not self.cfg.get("llm_controller"):
            raise RuntimeError(f"本地翻译服务 {self.url} 没有运行。请先启动它，或在 config.json 里设置 llm_controller 让本程序自动启动。")
        progress(f"正在启动翻译模型 {self.label}（约 25 秒）…")
        try:
            self._control("start", timeout=300)
            self.owned = True
            deadline = time.time() + 120
            while not self.ready():
                if time.time() > deadline:
                    raise TimeoutError(f"翻译服务已启动，但 {self.url} 未就绪")
                time.sleep(0.5)
        except BaseException:
            try:
                self._control("stop", timeout=60)
            except Exception:
                pass
            self.owned = False
            raise

    def stop(self) -> None:
        if self.owned:
            try:
                self._control("stop", timeout=90)
            finally:
                self.owned = False

    def translate(self, source: str, context: str, topic: str, terms: str, context_chars: int = 160, *, on_partial=None, cancelled=None) -> dict:
        scene = topic.strip() or "Teams 工作会议"
        system = ("将德语口语译成简体中文，只输出当前语句译文。保留数字、否定、疑问和残句，不补写。"
                  f"场景是{scene}。只纠正上下文明显支持的识别错词。")
        if terms.strip():
            system += f"人名和术语：{terms.strip()}。"
        user = ("前文仅供理解：" + context[-context_chars:] + "\n\n" if context else "") + "当前语句：\n" + source
        body = {"model": self.cfg["llm_model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0, "max_tokens": 256, "stream": True, "chat_template_kwargs": {"enable_thinking": False}}
        text, seconds, finish = _stream_chat(self.url + "/v1/chat/completions", body, on_partial=on_partial, cancelled=cancelled)
        return {"translation": text, "mt_s": seconds, "finish_reason": finish}


class HyMT7BTranslator:
    name = "hymt7b"
    label = "Hy-MT2-7B（省显存）"

    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.port = int(cfg.get("hymt_port") or 18082)
        self.url = f"http://127.0.0.1:{self.port}"

    def ready(self) -> bool:
        return _http_ok(self.url + "/health")

    def start(self, progress) -> None:
        if self.proc and self.proc.poll() is None and self.ready():
            return
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", self.port)) == 0:
                raise RuntimeError(f"端口 {self.port} 已被其他程序占用")
        server, model = Path(self.cfg.get("llama_server") or ""), Path(self.cfg.get("hymt_model") or "")
        for p, key in ((server, "llama_server"), (model, "hymt_model")):
            if not p.is_file():
                raise FileNotFoundError(f"省显存模式缺少文件（config.json 的 {key}）：{p}")
        progress("正在加载翻译模型 Hy-MT2-7B…")
        logf = open(config.LOG_DIR / "llama-hymt7b.log", "w", encoding="utf-8")
        args = [str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(self.port),
                "-ngl", "all", "-c", "4096", "-np", "1", "-fa", "on", "--fit", "off", "--jinja", "--reasoning", "off", "--no-webui"]
        self.proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        deadline = time.time() + 120
        while not self.ready():
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 退出（{self.proc.returncode}），见 logs/llama-hymt7b.log")
            if time.time() > deadline:
                self.stop()
                raise TimeoutError("Hy-MT2-7B 加载超时")
            time.sleep(0.3)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def translate(self, source: str, context: str, topic: str, terms: str, context_chars: int = 240, *, on_partial=None, cancelled=None) -> dict:
        background = f"这是一场{topic.strip() or 'Teams 工作会议'}的德语发言。"
        if terms.strip():
            background += f"\n人名和术语：{terms.strip()}"
        if context:
            background += "\n前文：" + context[-240:]
        prompt = "[背景信息]\n" + background + "\n\n请结合背景信息将以下文本翻译为简体中文。只输出译文。\n\n[待翻译文本]\n" + source
        body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0, "top_p": 1, "max_tokens": 512, "stream": True, "seed": 42,
                "chat_template_kwargs": {"enable_thinking": False}}
        text, seconds, finish = _stream_chat(self.url + "/v1/chat/completions", body, on_partial=on_partial, cancelled=cancelled)
        return {"translation": text, "mt_s": seconds, "finish_reason": finish}


TRANSLATORS = {"qwen": LLMTranslator, "hymt7b": HyMT7BTranslator}
