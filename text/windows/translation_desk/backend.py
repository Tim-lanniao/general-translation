"""Everything the desk does outside its window, with the standard library only.

It uses the other components through their published entry points: the gateway's launcher and
HTTP interface (docs/gateway-api.md), and the audio app's command line. It imports neither.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import paths

SERVICE = "translation-gateway"
DEFAULT_URL = "http://127.0.0.1:18083"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
# The desk's own translations are not kept in the gateway's disk cache (docs/gateway-api.md).
REQUEST_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store"}

SOURCES = {"auto": "自动检测", "de": "德语", "en": "英语", "ja": "日语", "fr": "法语", "es": "西班牙语"}
TARGETS = {"zh": "简体中文", "de": "德语", "en": "英语"}
INSTRUCTION = "把用户发来的文字译成{target}。只输出译文，保留数字、人名、型号和段落格式，不解释。"
SOURCE_HINT = "原文是{source}。"
# The gateway's translation prompt labels the sentence 「当前语句」; a model may echo that label or
# answer with 「译文：」. Only these two are removed, and only from /v1/translate answers.
_LABELS = ("当前语句", "译文")
_LEAK = re.compile(r"^(?:当前语句|译文)\s*[:：]\s*")
CAPTION_STATES = ("running", "paused", "loading", "stopping")


class DeskError(RuntimeError):
    """A failure the window shows in full. The log gets only ``stage`` and ``status``,
    because a message from the model server may quote the text being translated."""

    def __init__(self, message: str, *, stage: str = "desk", status: int | None = None, interrupted: bool = False):
        super().__init__(message)
        # interrupted: the answer broke off mid-way; the next paragraph may still work
        self.stage, self.status, self.interrupted = stage, status, interrupted

    def log_line(self) -> str:
        return f"{self.stage} failed" + (f" (HTTP {self.status})" if self.status else "")


def child_env() -> dict:
    """Programs the desk starts get its data root as an absolute path, whatever their working directory."""
    return dict(os.environ, TRANSLATION_DATA=str(paths.ROOT))


def clean(text: str) -> str:
    return _LEAK.sub("", text.strip()).strip()


@dataclass(frozen=True)
class Piece:
    text: str
    new_paragraph: bool  # False: the rest of an overlong paragraph


def split_paragraphs(text: str, limit: int = 1200) -> list[Piece]:
    """One request per paragraph keeps each answer within the gateway's 512 tokens. Blank lines separate
    paragraphs; an overlong paragraph is cut after a sentence, else at a space, never inside a word."""
    pieces = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block, first = block.strip(), True
        while len(block) > limit:
            cut = max(block.rfind(mark, 0, limit) for mark in (". ", "? ", "! ", "。", "？", "！", "\n"))
            if cut > limit // 3:
                cut += 1
            else:
                space = block.rfind(" ", limit // 3, limit)
                cut = space if space > 0 else limit  # e.g. Chinese without punctuation
            pieces.append(Piece(block[:cut].strip(), first))
            block, first = block[cut:].strip(), False
        if block:
            pieces.append(Piece(block, first))
    return pieces


def join_pieces(pieces: list[Piece], results: list[str], target: str) -> str:
    """Paragraphs stay separated by a blank line; the parts of one paragraph become one paragraph again."""
    paragraphs = []
    glue = "" if target == "zh" else " "
    for piece, result in zip(pieces, results):
        if piece.new_paragraph or not paragraphs:
            paragraphs.append(result)
        else:
            paragraphs[-1] += glue + result
    return "\n\n".join(paragraphs)


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


def caption_state(status: dict | None) -> str:
    """What the audio app is doing, from its caption sessions on the gateway: stopped, loading (the session
    exists, Whisper is still loading), running, paused, stopping, or unknown when the gateway did not answer."""
    if status is None:
        return "unknown"
    if not status.get("caption_sessions"):
        return "stopped"
    states = status.get("caption_states") or {}
    return next((name for name in CAPTION_STATES if states.get(name)), "loading")


def _abort(sock) -> None:
    """Close a socket another thread is blocked on. Windows only wakes that read when the handle is closed,
    and a plain close() waits for the HTTP response's file object, so the handle is detached and closed."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        socket.socket(fileno=sock.detach()).close()
    except OSError:
        pass


class Call:
    """Cancels a streaming request at any point, also while it queues or the model has not answered yet;
    closing the connection makes the gateway drop the request and free its slot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._connection = None
        self.cancelled = False

    def attach(self, connection) -> None:
        with self._lock:
            if self.cancelled:
                raise CancelledError()
            self._connection = connection

    def detach(self) -> None:
        with self._lock:
            self._connection = None

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            connection = self._connection
        if connection is not None:
            _abort(connection.sock)


class Gateway:
    def __init__(self, url: str | None = None, launcher: Path | None = None):
        configured = url or os.environ.get("TRANSLATION_GATEWAY_URL")
        self.url = (configured or DEFAULT_URL).rstrip("/")
        # Without an explicit URL only the gateway's launcher knows the configured port; another gateway
        # may answer on the default one.
        self.explicit = self.resolved = bool(configured)
        self.launcher = Path(launcher or paths.GATEWAY_LAUNCHER)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self, timeout: float = 90) -> str:
        """Make sure the configured gateway runs and learn its address from its launcher."""
        if self.explicit and self.status() is not None:
            return self.url
        if not self.launcher.is_file():
            raise DeskError(f"找不到翻译网关启动器：{self.launcher}", stage="gateway start")
        try:
            out = subprocess.run(["cmd.exe", "/d", "/c", str(self.launcher), "start"], capture_output=True, env=child_env(),
                                 timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise DeskError("翻译网关启动超时", stage="gateway start") from None
        text = (out.stdout + out.stderr).decode("utf-8", "replace").strip()
        if out.returncode != 0:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            raise DeskError(" ".join(lines)[:400] if lines else f"翻译网关启动失败（exit {out.returncode}）",
                            stage="gateway start")
        urls = re.findall(r"https?://[^\s]+", text)
        if not self.explicit:
            if not urls:
                raise DeskError("翻译网关启动器没有给出地址", stage="gateway start")
            self.url = urls[-1].rstrip("/")
        self.resolved = True
        return self.url

    def status(self) -> dict | None:
        try:
            with self._opener.open(self.url + "/status", timeout=2) as response:
                result = json.load(response)
        except (OSError, ValueError):
            return None
        return result if isinstance(result, dict) and result.get("service") == SERVICE else None

    def stream(self, path: str, body: dict, *, on_partial=None, call: Call | None = None, timeout: float = 420,
               labels: bool = False) -> tuple[str, str | None]:
        """POST a streaming request and return (text, finish_reason). A stream that ends without its
        end marker or finish reason is an interrupted translation and raises DeskError."""
        call = call or Call()
        target = urlsplit(self.url)
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
        raw, shown, finish, done, answering = "", "", None, False, False
        try:
            call.attach(connection)
            connection.request("POST", target.path.rstrip("/") + path,
                               json.dumps(dict(body, stream=True), ensure_ascii=False).encode("utf-8"), REQUEST_HEADERS)
            if call.cancelled:
                raise CancelledError()
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read(4000).decode("utf-8", "replace")
                try:
                    detail = json.loads(detail)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    pass
                raise DeskError(f"翻译网关 HTTP {response.status}：{detail}", stage="translate", status=response.status)
            answering = True
            for line in response:
                if call.cancelled:
                    raise CancelledError()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                event = json.loads(data)
                if event.get("error"):
                    raise DeskError(f"翻译中断：{event['error']}", stage="translate", interrupted=True)
                for choice in event.get("choices") or []:
                    finish = choice.get("finish_reason") or finish
                    delta = (choice.get("delta") or {}).get("content")
                    if not delta:
                        continue
                    raw += delta
                    if labels and any(label.startswith(raw.strip()) for label in _LABELS):
                        continue  # a label may arrive one character at a time
                    text = clean(raw) if labels else raw.strip()
                    if on_partial and text and text != shown:
                        on_partial(text)
                        shown = text
        except OSError as exc:
            if call.cancelled:
                raise CancelledError() from None
            reason = getattr(exc, "strerror", None) or exc
            if answering:
                raise DeskError(f"与翻译网关的连接中断：{reason}", stage="translate", interrupted=True) from None
            raise DeskError(f"无法连接翻译网关：{reason}", stage="translate") from None
        finally:
            call.detach()
            connection.close()
        if not done and finish is None:
            raise DeskError("翻译中断：网关在译文完成前断开了连接", stage="translate", interrupted=True)
        if finish not in (None, "stop", "length"):
            raise DeskError(f"翻译没有正常结束（{finish}）", stage="translate", interrupted=True)
        text = clean(raw) if labels else raw.strip()
        if not text:
            raise DeskError("网关返回了空译文", stage="translate", interrupted=True)
        return text, finish

    def translate(self, text: str, source: str = "auto", target: str = "zh", *, context: str = "",
                  on_partial=None, call: Call | None = None) -> tuple[str, str | None]:
        if target == "zh":  # the gateway's own translation prompt, with the shared glossary
            body = {"source": text, "source_language": source, "context": context[-512:]}
            return self.stream("/v1/translate", body, on_partial=on_partial, call=call, labels=True)
        instruction = INSTRUCTION.format(target=TARGETS[target])
        if source != "auto":  # the chosen language settles words that exist in several languages
            instruction = SOURCE_HINT.format(source=SOURCES[source]) + instruction
        messages = [{"role": "system", "content": instruction}, {"role": "user", "content": text}]
        return self.stream("/v1/chat/completions", {"messages": messages}, on_partial=on_partial, call=call)

    def warm(self) -> None:
        """Load the model with a one-line translation. temperature > 0 keeps it out of the cache."""
        self.stream("/v1/translate", {"source": "Guten Morgen.", "source_language": "de", "temperature": 0.1, "max_tokens": 32},
                    labels=True)


class Audio:
    """The audio caption app, controlled through its command line. Opening and closing run one at a time."""

    def __init__(self, directory: Path | None = None, python: str | None = None):
        self.dir = Path(directory or paths.AUDIO_DIR)
        self.python = Path(python or os.environ.get("AUDIO_CAPTION_PYTHON") or self.dir / ".venv" / "Scripts" / "pythonw.exe")
        self.process = None  # the app this desk started, until it exits
        self._lock = threading.Lock()

    def _check(self) -> None:
        if not self.python.is_file():
            raise DeskError(f"找不到音频翻译的 Python 环境：{self.python}", stage="audio")

    def open(self) -> None:
        """Start captions: a new app loads Whisper and starts; a running app starts its captions again."""
        with self._lock:
            self._check()
            self.process = subprocess.Popen([str(self.python), "-m", "audio_caption", "--start"], cwd=self.dir,
                                            env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=DETACHED, close_fds=True)

    def close(self, timeout: float = 60, retry_after: float = 8) -> None:
        """Ask the app to stop captions, release Whisper and exit. An app this desk has just started may not
        listen yet, so the request is repeated until that process has exited."""
        with self._lock:
            self._check()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeskError("音频翻译没有退出，请在它的窗口里关闭", stage="audio")
                try:
                    out = subprocess.run([str(self.python), "-m", "audio_caption", "--quit"], cwd=self.dir, env=child_env(),
                                         capture_output=True, timeout=remaining, creationflags=NO_WINDOW,
                                         stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    raise DeskError("音频翻译没有响应退出请求", stage="audio") from None
                if out.returncode != 0:
                    raise DeskError(f"音频翻译的退出命令失败（exit {out.returncode}）", stage="audio")
                if self.process is None:
                    return
                try:
                    self.process.wait(timeout=max(.1, min(retry_after, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    continue
                self.process = None
                return
