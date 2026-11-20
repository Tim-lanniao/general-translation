"""The desk window: text translation by default, audio captions only when asked for.

Starting the desk starts the gateway and warms the model; Whisper stays unloaded until
「打开音频翻译」 starts the audio app. Closing the window keeps the desk in the tray.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from itertools import accumulate

from PySide6.QtCore import QDir, QEvent, QLockFile, QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QTextCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import backend, paths

log = logging.getLogger("translation_desk")
APP_KEY = "TranslationDesk-" + os.environ.get("USERNAME", "user")
COMMANDS = {"--open-audio": b"open-audio", "--close-audio": b"close-audio", "--quit": b"quit"}
AUDIO_OPEN_SECONDS = 120   # Whisper alone takes a few seconds; a cold Qwen adds about 25
AUDIO_CLOSE_SECONDS = 90
STATUS_MISSES = 3          # unanswered status requests before the gateway counts as gone
ACTIVE = ("loading", "running", "paused")

STYLE = """
QWidget#root { background: #F3F6F4; }
QLabel { color: #23332C; }
QLabel#title { font: 700 20px 'Microsoft YaHei UI'; color: #1D4034; }
QLabel#muted { color: #6A7D73; font-size: 12px; }
QLabel#warning { color: #B3402F; font-size: 12px; }
QLabel#section { font-weight: 600; color: #1D4034; }
QFrame#card { background: #FFFFFF; border: 1px solid #DCE5DF; border-radius: 12px; }
QPlainTextEdit { border: 1px solid #DCE5DF; border-radius: 8px; padding: 6px; background: #FFFFFF;
                 font-size: 15px; selection-background-color: #CFE5D8; selection-color: #1D4034; }
QPlainTextEdit#output { background: #FAFCFA; }
QPushButton { border: 1px solid #C9D6CE; border-radius: 8px; padding: 6px 14px; background: #FFFFFF; color: #1D4034; }
QPushButton:hover { background: #EEF4F0; }
QPushButton#primary { background: #24614A; color: #FFFFFF; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #1D5440; }
QPushButton:disabled { color: #9AA8A0; background: #F3F6F4; border-color: #DCE5DF; }
QComboBox { border: 1px solid #C9D6CE; border-radius: 6px; padding: 3px 8px; background: #FFFFFF; min-width: 88px; }
"""
STATE_COLORS = {"ready": "#1F7A4D", "error": "#B3402F", "offline": "#B3402F"}


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(0, 0, size, size), size * .22, size * .22)
        painter.fillPath(shape, QColor("#24614A"))
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(8, int(size * .62)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "译")
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _log_reason(exc: Exception) -> str:
    """What may go to the log file: never the message, which can quote the text being translated."""
    return exc.log_line() if isinstance(exc, backend.DeskError) else type(exc).__name__


class Bridge(QObject):
    """Worker threads talk to the window through these signals."""
    status = Signal(object)            # GET /status, or None
    startup = Signal(str, bool)        # message ("" when done), is_error
    partial = Signal(int, str)         # job, translation so far
    done = Signal(int, str, str, bool)  # job, translation, note, whether something is missing
    audio_done = Signal(str)           # "" or what went wrong


class DeskWindow(QWidget):
    def __init__(self, gateway: backend.Gateway, audio: backend.Audio, settings: dict, *, warm: bool = True, start: bool = True):
        super().__init__()
        self.gateway, self.audio, self.settings = gateway, audio, settings
        self.bridge = Bridge()
        self.status = None
        self.misses = 0
        self.job, self.call = 0, None
        self.startup_note, self.startup_error = "", False
        self.audio_state = "stopped"  # last caption state the gateway reported
        self.audio_pending = None     # ("opening" | "closing", deadline): the last request, until the state matches
        self.audio_busy = 0           # audio commands still running
        self.audio_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desk-audio")
        self.audio_note = ""
        self.polling = False
        self.quitting = False
        self.tray = None
        self.setObjectName("root")
        self.setWindowTitle("通用翻译")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLE)
        self._build()
        self._restore_geometry()
        self.bridge.status.connect(self.on_status)
        self.bridge.startup.connect(self.on_startup)
        self.bridge.partial.connect(self.on_partial)
        self.bridge.done.connect(self.on_done)
        self.bridge.audio_done.connect(self.on_audio_done)
        self._build_tray()
        self._show_audio()
        self.poll_timer = QTimer(self, interval=2000, timeout=self.poll)
        if start:
            self.poll_timer.start()
            threading.Thread(target=self._start_up, args=(warm,), name="desk-startup", daemon=True).start()

    # ------------------------------------------------------------------ layout
    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return frame, layout

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        title = QLabel("通用翻译")
        title.setObjectName("title")
        self.model_label = QLabel("正在启动…")
        self.model_label.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(self.model_label)

        card, layout = self._card()
        row = QHBoxLayout()
        row.addWidget(QLabel("原文"))
        self.source_box = QComboBox()
        for code, name in backend.SOURCES.items():
            self.source_box.addItem(name, code)
        row.addWidget(self.source_box)
        row.addWidget(QLabel("译成"))
        self.target_box = QComboBox()
        for code, name in backend.TARGETS.items():
            self.target_box.addItem(name, code)
        row.addWidget(self.target_box)
        row.addStretch(1)
        self.translate_button = QPushButton("翻译")
        self.translate_button.setObjectName("primary")
        self.translate_button.setToolTip("Ctrl + Enter")
        self.translate_button.clicked.connect(self.translate)
        row.addWidget(self.translate_button)
        layout.addLayout(row)
        self.source_edit = QPlainTextEdit()
        self.source_edit.setPlaceholderText("粘贴或输入要翻译的文字，按 Ctrl + Enter 翻译。空行分隔的段落会逐段翻译。")
        layout.addWidget(self.source_edit, 2)
        self.output = QPlainTextEdit()
        self.output.setObjectName("output")
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("译文会逐字出现在这里")
        layout.addWidget(self.output, 3)
        row = QHBoxLayout()
        self.note_label = QLabel("")
        self.note_label.setObjectName("muted")
        self.note_label.setWordWrap(True)
        row.addWidget(self.note_label, 1)
        self.copy_button = QPushButton("复制译文")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_output)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        root.addWidget(card, 1)

        card, layout = self._card()
        row = QHBoxLayout()
        self.audio_label = QLabel()
        self.audio_label.setObjectName("section")
        self.audio_label.setWordWrap(True)
        row.addWidget(self.audio_label, 1)
        self.audio_button = QPushButton()
        self.audio_button.clicked.connect(self.toggle_audio)
        row.addWidget(self.audio_button)
        layout.addLayout(row)
        self.audio_hint = QLabel()
        self.audio_hint.setObjectName("muted")
        self.audio_hint.setWordWrap(True)
        layout.addWidget(self.audio_hint)
        root.addWidget(card)

        self.footer = QLabel()
        self.footer.setObjectName("muted")
        self.footer.setWordWrap(True)
        root.addWidget(self.footer)
        self._show_footer()

        source = self.source_box.findData(self.settings.get("source"))
        self.source_box.setCurrentIndex(max(0, source))
        target = self.target_box.findData(self.settings.get("target"))
        self.target_box.setCurrentIndex(max(0, target))
        self.source_edit.installEventFilter(self)  # Ctrl + Enter translates; a window shortcut would lose to the editor

    def eventFilter(self, watched, event):
        if (watched is self.source_edit and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.translate()
            return True
        return super().eventFilter(watched, event)

    def _show_footer(self):
        address = f"{self.gateway.url}/v1" if self.gateway.resolved else "网关的地址"
        self.footer.setText(f"关闭窗口后在托盘继续运行。浏览器扩展、Pot 等工具可直接使用 {address}，API key 填 local。")

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("通用翻译")
        menu = QMenu(self)
        show = QAction("显示窗口", self, triggered=self.bring_to_front)
        self.tray_audio = QAction("打开音频翻译", self, triggered=self.toggle_audio)
        leave = QAction("退出", self, triggered=self.quit_now)
        menu.addAction(show)
        menu.addAction(self.tray_audio)
        menu.addSeparator()
        menu.addAction(leave)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.bring_to_front()

    def _restore_geometry(self):
        self.resize(560, 760)
        geometry = self.settings.get("geometry")
        if geometry and any(screen.availableGeometry().intersects(QRect(*geometry)) for screen in QApplication.screens()):
            self.setGeometry(*geometry)

    # ------------------------------------------------------------------ gateway
    def _start_up(self, warm: bool):
        started = time.perf_counter()
        try:
            self.bridge.startup.emit("正在启动翻译网关…", False)
            log.info("gateway at %s", self.gateway.start())
            self.bridge.status.emit(self.gateway.status())
            if warm:
                self.bridge.startup.emit("正在加载翻译模型（约 25 秒）…", False)
                self.gateway.warm()
            self.bridge.startup.emit("", False)
            log.info("text mode ready after %.1f s", time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            log.warning("start-up: %s", _log_reason(exc))
            self.bridge.startup.emit(str(exc), True)
        self.poll()

    def poll(self):
        if self.polling or not (self.gateway.resolved or self.gateway.explicit):
            return
        self.polling = True

        def work():
            try:
                self.bridge.status.emit(self.gateway.status())
            finally:
                self.polling = False

        threading.Thread(target=work, name="desk-status", daemon=True).start()

    def on_startup(self, message: str, error: bool):
        self.startup_note, self.startup_error = message, error
        self._show_footer()
        self._show_model()

    def on_status(self, status):
        if status is None:
            self.misses += 1
            if self.status is not None and self.misses < STATUS_MISSES:
                return  # one slow answer is no reason to show the gateway or the captions as gone
        else:
            self.misses = 0
        self.status = status
        if self.startup_error and backend.describe(status)[0] == "ready":
            self.startup_note, self.startup_error = "", False
        self._show_model()
        state = backend.caption_state(status)
        if state != "unknown":
            self.audio_state = state
        self._settle_audio(state)
        self._show_audio()

    def _show_model(self):
        state, line = backend.describe(self.status)
        if self.startup_note:
            state, line = ("error" if self.startup_error else "starting"), self.startup_note
        self.model_label.setText(line)
        self.model_label.setStyleSheet(f"color: {STATE_COLORS.get(state, '#4A5C53')}; font-size: 14px;")

    # ------------------------------------------------------------------ text translation
    def translate(self):
        text = self.source_edit.toPlainText().strip()
        if not text:
            self.source_edit.setFocus()
            return
        if self.call:  # the previous request leaves the queue or the model at once
            self.call.cancel()
        self.job += 1
        job, call = self.job, backend.Call()
        self.call = call
        source, target = self.source_box.currentData(), self.target_box.currentData()
        pieces = backend.split_paragraphs(text)
        paragraphs = list(accumulate(int(p.new_paragraph) for p in pieces))
        self.output.clear()
        self.copy_button.setEnabled(False)
        self._note("翻译中…" if paragraphs[-1] == 1 else f"翻译中 · 共 {paragraphs[-1]} 段")

        def work():
            results, broken, started = [], [], time.perf_counter()
            try:
                if not self.gateway.resolved or self.gateway.status() is None:
                    self.gateway.start()
                for i, piece in enumerate(pieces):
                    partial_text = ""

                    def partial(text_so_far):
                        nonlocal partial_text
                        partial_text = text_so_far
                        self.bridge.partial.emit(job, backend.join_pieces(pieces, [*results, text_so_far], target))

                    try:
                        result, finish = self.gateway.translate(piece.text, source, target, on_partial=partial, call=call,
                                                                context=pieces[i - 1].text if i else "")
                        if finish == "length":
                            result += f"〔第 {paragraphs[i]} 段过长，译文在这里被截断〕"
                            broken.append(paragraphs[i])
                    except backend.DeskError as exc:
                        log.warning("piece %d of %d: %s", i + 1, len(pieces), _log_reason(exc))
                        broken.append(paragraphs[i])
                        if not exc.interrupted:  # the gateway cannot be reached: the remaining text stays untranslated
                            results.append(f"〔第 {paragraphs[i]} 段起没有翻译：{exc}〕")
                            pieces[:] = pieces[:len(results)]
                            break
                        result = f"{partial_text}〔第 {paragraphs[i]} 段翻译中断：{exc}〕"
                    results.append(result)
                    self.bridge.partial.emit(job, backend.join_pieces(pieces, results, target))
            except CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("translation: %s", _log_reason(exc))
                self.bridge.done.emit(job, backend.join_pieces(pieces, results, target), f"翻译出错：{exc}", True)
                return
            seconds = time.perf_counter() - started
            log.info("translated %d characters in %d piece(s), %.2f s, %d incomplete", len(text), len(pieces), seconds, len(broken))
            note = f"用时 {seconds:.1f} 秒"
            if broken:
                listed = "、".join(str(n) for n in sorted(set(broken)))
                note = f"第 {listed} 段没有完整译文，已在译文里用〔〕标出 · {note}"
            self.bridge.done.emit(job, backend.join_pieces(pieces, results, target), note, bool(broken))

        threading.Thread(target=work, name="desk-translate", daemon=True).start()

    def _note(self, text: str, warning: bool = False):
        self.note_label.setText(text)
        name = "warning" if warning else "muted"
        if self.note_label.objectName() != name:
            self.note_label.setObjectName(name)
            self.note_label.style().unpolish(self.note_label)
            self.note_label.style().polish(self.note_label)

    def on_partial(self, job: int, text: str):
        if job != self.job:
            return
        self.output.setPlainText(text)
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def on_done(self, job: int, text: str, note: str, incomplete: bool):
        if job != self.job:
            return
        self.output.setPlainText(text)
        self._note(note, incomplete)
        self.copy_button.setEnabled(bool(text))

    def copy_output(self):
        QApplication.clipboard().setText(self.output.toPlainText())
        if self.note_label.objectName() != "warning":
            self._note("译文已复制")

    # ------------------------------------------------------------------ audio captions
    def _settle_audio(self, state: str):
        """A request counts as done once its command has run and the gateway shows the requested state."""
        if not self.audio_pending or self.audio_busy:
            return
        kind, deadline = self.audio_pending
        error = self.audio.open_error() if kind == "opening" else ""
        if error:
            self.audio_pending = None
            self.audio_note = error
        elif (kind == "opening" and state == "running") or (kind == "closing" and state == "stopped"):
            self.audio_pending = None
        elif kind == "opening" and state == "loading":
            return  # Whisper or a cold Qwen is still loading
        elif time.monotonic() > deadline:
            self.audio_pending = None
            if state == "unknown":
                self.audio_state = "unknown"
            self.audio_note = ("音频翻译没有开始字幕，请在它的窗口里查看。" if kind == "opening"
                               else "音频翻译还没有退出，请在它的窗口里关闭。")

    def _show_audio(self):
        state = self.audio_state
        pending = self.audio_pending[0] if self.audio_pending else None
        if pending == "closing" or state == "stopping":
            label, button, enabled, primary = "正在关闭音频翻译，释放 Whisper…", "正在关闭…", False, False
        elif pending == "opening" or state == "loading":
            label, button, enabled, primary = "正在打开音频翻译，加载 Whisper…", "取消", True, False
        elif state == "running":
            label, button, enabled, primary = "音频翻译：字幕运行中，Whisper 已加载", "关闭音频翻译", True, False
        elif state == "paused":
            label, button, enabled, primary = "音频翻译：字幕已暂停，Whisper 仍在显存中", "关闭音频翻译", True, False
        elif state == "unknown":
            label, button, enabled, primary = "音频翻译：无法确认字幕状态", "打开音频翻译", True, True
        else:
            label, button, enabled, primary = "音频翻译：未运行，不占用 Whisper 显存", "打开音频翻译", True, True
        self.audio_label.setText(label)
        self.audio_button.setText(button)
        self.audio_button.setEnabled(enabled)
        name = "primary" if primary else ""
        if self.audio_button.objectName() != name:
            self.audio_button.setObjectName(name)
            self.audio_button.style().unpolish(self.audio_button)  # apply the style of the new object name
            self.audio_button.style().polish(self.audio_button)
        hint = self.audio_note or ("字幕在独立的音频翻译窗口里，关闭它会释放 Whisper，文本翻译不受影响。" if state in ACTIVE
                                   else "打开后会加载 Whisper 并开始实时中文字幕。")
        if not self.audio_note and self.status is None and self.gateway.resolved and self.misses:
            hint = "暂时读不到网关状态，这里显示的是最后一次的状态。"
        self.audio_hint.setText(hint)
        if self.tray:
            self.tray_audio.setText(button)
            self.tray_audio.setEnabled(enabled)

    def toggle_audio(self):
        pending = self.audio_pending[0] if self.audio_pending else None
        if pending == "closing":
            return
        if pending == "opening" or self.audio_state in ACTIVE:
            self.close_audio()
        else:
            self.open_audio()

    def open_audio(self):
        pending = self.audio_pending[0] if self.audio_pending else None
        if pending == "opening" or (pending is None and self.audio_state in ("loading", "running")):
            return
        self.audio_note = ""
        self.audio_pending = ("opening", time.monotonic() + AUDIO_OPEN_SECONDS)
        self._show_audio()
        log.info("opening audio captions")
        self._in_background(lambda: self.audio.open(self.gateway.start()))

    def close_audio(self):
        if self.audio_pending and self.audio_pending[0] == "closing":
            return
        self.audio_note = ""
        self.audio_pending = ("closing", time.monotonic() + AUDIO_CLOSE_SECONDS)
        self._show_audio()
        log.info("closing audio captions")
        self._in_background(self.audio.close)

    def _in_background(self, action):
        """Submit from the UI thread to a single FIFO worker, including commands relayed from the tray."""
        self.audio_busy += 1

        def work():
            error = ""
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                log.warning("audio control: %s", _log_reason(exc))
                error = str(exc)
            self.bridge.audio_done.emit(error)

        self.audio_worker.submit(work)

    def on_audio_done(self, error: str):
        self.audio_busy -= 1
        if self.quitting and not self.audio_busy:
            if error and self.audio_pending and self.audio_pending[0] == "closing":
                self.quitting = False
                self.setEnabled(True)
                self.bring_to_front()
            else:
                self.close()
                return
        if error and not self.audio_busy:
            self.audio_pending = None
            self.audio_note = error
        self._show_audio()
        self.poll()

    # ------------------------------------------------------------------ window
    def handle_command(self, command: bytes):
        if self.quitting:
            return
        if command == b"open-audio":
            self.open_audio()
        elif command == b"close-audio":
            self.close_audio()
        elif command == b"quit":
            self.quit_now()
        else:
            self.bring_to_front()

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _save(self):
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        self.settings.update(source=self.source_box.currentData(), target=self.target_box.currentData(),
                             geometry=[geometry.x(), geometry.y(), geometry.width(), geometry.height()])
        try:
            paths.save_settings(self.settings)
        except OSError as exc:
            log.warning("settings not saved: %s", type(exc).__name__)

    def quit_now(self):
        self.quitting = True
        self.close()

    def closeEvent(self, event):
        self._save()
        if self.tray and not self.quitting:
            event.ignore()
            self.hide()
            if not self.settings.get("tray_hint_shown"):
                self.tray.showMessage("通用翻译", "仍在托盘里运行，右键托盘图标可以退出。", self.windowIcon(), 4000)
                self.settings["tray_hint_shown"] = True
                self._save()
            return
        self.quitting = True
        if self.call:
            self.call.cancel()
        if self.audio_busy:
            event.ignore()  # on_audio_done finishes closing after the last submitted command
            self.setEnabled(False)
            return
        event.accept()
        self.audio_worker.shutdown()
        self.poll_timer.stop()
        log.info("desk closed; the gateway keeps serving other clients")
        QApplication.quit()


def setup_logging():
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(paths.LOG_DIR / "translation-desk.log", maxBytes=1_000_000,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def send_to_running_desk(command: bytes, seconds: float = 5) -> bool:
    """The desk that holds the lock may still be starting its command server, so keep trying for a moment."""
    deadline = time.monotonic() + seconds
    while True:
        socket = QLocalSocket()
        socket.connectToServer(APP_KEY)
        if socket.waitForConnected(200):
            socket.write(command)
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(.1)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((COMMANDS[arg] for arg in argv if arg in COMMANDS), b"show")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("通用翻译")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(False)

    # One desk per user. The lock decides atomically which process becomes the desk (two quick double-clicks
    # cannot both pass); every other start hands its command to that desk.
    lock = QLockFile(os.path.join(QDir.tempPath(), APP_KEY + ".lock"))
    lock.setStaleLockTime(0)  # only a lock whose process has gone is stale
    if not lock.tryLock(0):
        if send_to_running_desk(command):
            return 0
        if command in (b"quit", b"close-audio"):
            return 0 if command == b"quit" else 1
        QMessageBox.warning(None, "通用翻译", "另一个通用翻译窗口正在启动或没有响应，请稍后再试。")
        return 1
    if command == b"quit":
        return 0
    if command == b"close-audio":  # no desk running: just ask the audio app to quit
        try:
            backend.Audio().close()
        except backend.DeskError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    setup_logging()
    QLocalServer.removeServer(APP_KEY)
    server = QLocalServer()
    if not server.listen(APP_KEY):
        raise RuntimeError(f"无法建立命令通道：{server.errorString()}")
    window = DeskWindow(backend.Gateway(), backend.Audio(), paths.load_settings(), warm="--no-warm" not in argv)

    def on_connection():
        connection = server.nextPendingConnection()
        message = bytes(connection.readAll()) if connection.waitForReadyRead(500) else b""
        window.handle_command(message.strip() or b"show")

    server.newConnection.connect(on_connection)
    window.show()
    if command == b"open-audio":
        QTimer.singleShot(0, window.open_audio)
    log.info("desk started")
    code = app.exec()
    lock.unlock()
    return code


def run():
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        log.error("fatal: %s", type(exc).__name__)
        try:
            QMessageBox.critical(None, "通用翻译出错", f"{type(exc).__name__}：{exc}\n\n日志：{paths.LOG_DIR}")
        except Exception:  # noqa: BLE001
            pass
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    run()
