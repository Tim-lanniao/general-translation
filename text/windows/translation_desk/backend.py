"""Everything the desk does outside its window, with the standard library only.

It uses the other components through their published entry points: the gateway's launcher and
HTTP interface (docs/gateway-api.md), and the audio app's command line. It imports neither.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import CancelledError
from pathlib import Path

from . import paths

SERVICE = "translation-gateway"
DEFAULT_URL = "http://127.0.0.1:18083"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

SOURCES = {"auto": "自动检测", "de": "德语", "en": "英语", "ja": "日语", "fr": "法语", "es": "西班牙语"}
TARGETS = {"zh": "简体中文", "de": "德语", "en": "英语"}
INSTRUCTION = "把用户发来的文字译成{}。只输出译文，保留数字、人名、型号和段落格式，不解释。"
# A model may label its answer ("译文：…"); the label is not part of the translation.
_LABELS = ("当前语句", "译文", "翻译", "中文")
_LEAK = re.compile(r"^(?:当前语句|译文|翻译|中文)\s*[:：]\s*")


class DeskError(RuntimeError):
    pass


def clean(text: str) -> str:
    return _LEAK.sub("", text.strip()).strip()


def split_paragraphs(text: str, limit: int = 1200) -> list[str]:
    """One request per paragraph keeps each answer within the gateway's 512 tokens.
    Blank lines separate paragraphs; an overlong paragraph is cut after a sentence."""
    pieces = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block = block.strip()
        while len(block) > limit:
            cut = max(block.rfind(mark, 0, limit) for mark in (". ", "? ", "! ", "。", "？", "！", "\n"))
            cut = cut + 1 if cut > limit // 3 else limit
            pieces.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            pieces.append(block)
    return pieces


def describe(status: dict | None) -> tuple[str, str]:
    """(model state, line for the window) from GET /status, or None while the gateway is down."""
    if status is None:
        return "offline", "翻译网关未运行"
    model = status.get("model") or {}
    label = model.get("label") or model.get("id") or "翻译模型"
    state = model.get("state") or "unknown"
    lines = {"ready": f"文本翻译就绪 · {label}",
             "loading": f"正在加载 {label}（约 25 秒）…",
             "unloading": f"正在卸载 {label}…",
             "unloaded": f"{label} 空闲时已卸载，下次翻译时自动加载（约 25 秒）"}
    return state, lines.get(state, "翻译网关运行中")


class Gateway:
    def __init__(self, url: str | None = None, launcher: Path | None = None):
        self.url = (url or os.environ.get("TRANSLATION_GATEWAY_URL") or DEFAULT_URL).rstrip("/")
        self.launcher = Path(launcher or paths.GATEWAY_LAUNCHER)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self, timeout: float = 90) -> str:
        """Make sure the gateway runs: use one that answers already, otherwise run the gateway's launcher,
        which starts it and prints its URL."""
        if self.status() is not None:
            return self.url
        if not self.launcher.is_file():
            raise DeskError(f"找不到翻译网关启动器：{self.launcher}")
        try:
            out = subprocess.run(["cmd.exe", "/d", "/c", str(self.launcher), "start"], capture_output=True,
                                 timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise DeskError("翻译网关启动超时") from None
        text = (out.stdout + out.stderr).decode("utf-8", "replace").strip()
        if out.returncode != 0:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            raise DeskError(" ".join(lines)[:400] if lines else f"翻译网关启动失败（exit {out.returncode}）")
        urls = re.findall(r"https?://[^\s]+", text)
        if urls:
            self.url = urls[-1].rstrip("/")
        return self.url

    def _open(self, path: str, body: dict | None = None, timeout: float = 3):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        return self._opener.open(request, timeout=timeout)

    def status(self) -> dict | None:
        try:
            with self._open("/status", timeout=2) as response:
                result = json.load(response)
        except (OSError, ValueError):
            return None
        return result if isinstance(result, dict) and result.get("service") == SERVICE else None

    def stream(self, path: str, body: dict, *, on_partial=None, cancelled=None, timeout: float = 420) -> tuple[str, str | None]:
        """POST a streaming request; returns (text, finish_reason). ``on_partial`` gets the cleaned text so far."""
        try:
            response = self._open(path, dict(body, stream=True), timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise DeskError(f"翻译网关 HTTP {exc.code}：{detail}") from None
        except OSError as exc:
            raise DeskError(f"无法连接翻译网关：{getattr(exc, 'reason', exc)}") from None
        raw, shown, finish = "", "", None
        with response:  # leaving early closes the connection, and the gateway stops the generation
            for line in response:
                if cancelled and cancelled():
                    raise CancelledError()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                event = json.loads(data)
                if event.get("error"):
                    raise DeskError(f"翻译中断：{event['error']}")
                for choice in event.get("choices") or []:
                    finish = choice.get("finish_reason") or finish
                    delta = (choice.get("delta") or {}).get("content")
                    if not delta:
                        continue
                    raw += delta
                    prefix = raw.strip()
                    if on_partial and not any(label.startswith(prefix) for label in _LABELS):
                        text = clean(raw)
                        if text and text != shown:
                            on_partial(text)
                            shown = text
        return clean(raw), finish

    def translate(self, text: str, source: str = "auto", target: str = "zh", *, context: str = "",
                  on_partial=None, cancelled=None) -> tuple[str, str | None]:
        if target == "zh":  # the gateway's own translation prompt, with the shared glossary
            body = {"source": text, "source_language": source, "context": context[-512:]}
            return self.stream("/v1/translate", body, on_partial=on_partial, cancelled=cancelled)
        messages = [{"role": "system", "content": INSTRUCTION.format(TARGETS[target])}, {"role": "user", "content": text}]
        return self.stream("/v1/chat/completions", {"messages": messages}, on_partial=on_partial, cancelled=cancelled)

    def warm(self) -> None:
        """Load the model with a one-line translation. temperature > 0 keeps it out of the cache."""
        self.stream("/v1/translate", {"source": "Guten Morgen.", "source_language": "de", "temperature": 0.1, "max_tokens": 32})


class Audio:
    """The audio caption app, controlled through its command line."""

    def __init__(self, directory: Path | None = None, python: str | None = None):
        self.dir = Path(directory or paths.AUDIO_DIR)
        self.python = Path(python or os.environ.get("AUDIO_CAPTION_PYTHON") or self.dir / ".venv" / "Scripts" / "pythonw.exe")

    def _check(self) -> None:
        if not self.python.is_file():
            raise DeskError(f"找不到音频翻译的 Python 环境：{self.python}")

    def open(self) -> None:
        """Start the caption app; it loads Whisper and starts captions. A running app comes to the front."""
        self._check()
        subprocess.Popen([str(self.python), "-m", "audio_caption"], cwd=self.dir, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=DETACHED, close_fds=True)

    def close(self, timeout: float = 60) -> None:
        """Ask a running caption app to stop captions, release Whisper and exit."""
        self._check()
        try:
            subprocess.run([str(self.python), "-m", "audio_caption", "--quit"], cwd=self.dir, capture_output=True,
                           timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise DeskError("音频翻译没有响应退出请求") from None

    @staticmethod
    def running(status: dict | None) -> bool:
        return bool(status and status.get("caption_sessions"))
