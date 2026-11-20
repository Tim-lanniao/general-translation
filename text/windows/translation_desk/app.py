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
from concurrent.futures import CancelledError

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QTextCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import backend, paths

log = logging.getLogger("translation_desk")
APP_KEY = "TranslationDesk-" + os.environ.get("USERNAME", "user")
COMMANDS = {"--open-audio": b"open-audio", "--close-audio": b"close-audio", "--quit": b"quit"}
AUDIO_OPEN_SECONDS = 120   # Whisper alone takes a few seconds; a cold Qwen adds about 25
AUDIO_CLOSE_SECONDS = 60

STYLE = """
QWidget#root { background: #F3F6F4; }
QLabel { color: #23332C; }
QLabel#title { font: 700 20px 'Microsoft YaHei UI'; color: #1D4034; }
QLabel#muted { color: #6A7D73; font-size: 12px; }
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


class Bridge(QObject):
    """Worker threads talk to the window through these signals."""
    status = Signal(object)        # GET /status, or None
    startup = Signal(str, bool)    # message ("" when done), is_error
    partial = Signal(int, str)     # job, translation so far
    done = Signal(int, str, str)   # job, translation, note
    audio_error = Signal(str)


class DeskWindow(QWidget):
    def __init__(self, gateway: backend.Gateway, audio: backend.Audio, settings: dict, *, warm: bool = True, start: bool = True):
        super().__init__()
        self.gateway, self.audio, self.settings = gateway, audio, settings
        self.bridge = Bridge()
        self.status = None
        self.job = 0
        self.startup_note, self.startup_error = "", False
        self.audio_pending = None   # ("opening" | "closing", deadline)
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
        self.bridge.audio_error.connect(self.on_audio_error)
        self._build_tray()
        self._show_audio(False)
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
        self.footer.setText(f"关闭窗口后在托盘继续运行。浏览器扩展、Pot 等工具可直接使用 {self.gateway.url}/v1，API key 填 local。")

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
            log.warning("start-up failed: %s", exc)
            self.bridge.startup.emit(str(exc), True)
        self.poll()

    def poll(self):
        if self.polling:
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
        self.status = status
        if self.startup_error and backend.describe(status)[0] == "ready":
            self.startup_note, self.startup_error = "", False
        self._show_model()
        running = backend.Audio.running(status)
        if self.audio_pending:
            kind, deadline = self.audio_pending
            if running == (kind == "opening"):
                self.audio_pending = None
            elif time.monotonic() > deadline:
                self.audio_pending = None
                self.audio_note = ("音频翻译没有开始字幕。若它的窗口已经打开，请在窗口里点「开始字幕」。" if kind == "opening"
                                   else "音频翻译还没有退出，请在它的窗口里关闭。")
        self._show_audio(running)

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
        self.job += 1
        job = self.job
        source, target = self.source_box.currentData(), self.target_box.currentData()
        pieces = backend.split_paragraphs(text)
        self.output.clear()
        self.copy_button.setEnabled(False)
        self.note_label.setText("翻译中…" if len(pieces) == 1 else f"翻译中 · 共 {len(pieces)} 段")

        def work():
            done, notes, started = [], [], time.perf_counter()
            try:
                if self.gateway.status() is None:
                    self.gateway.start()
                for i, piece in enumerate(pieces):
                    def partial(text_so_far, done=done):
                        self.bridge.partial.emit(job, "\n\n".join([*done, text_so_far]))

                    result, finish = self.gateway.translate(piece, source, target, context=pieces[i - 1] if i else "",
                                                            on_partial=partial, cancelled=lambda: job != self.job)
                    if finish == "length":
                        notes.append(f"第 {i + 1} 段过长，译文可能不完整")
                    done.append(result)
                    self.bridge.partial.emit(job, "\n\n".join(done))
                seconds = time.perf_counter() - started
                log.info("translated %d characters in %d piece(s), %.2f s", len(text), len(pieces), seconds)
                self.bridge.done.emit(job, "\n\n".join(done), " · ".join([f"用时 {seconds:.1f} 秒", *notes]))
            except CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("translation failed: %s", exc)
                self.bridge.done.emit(job, "\n\n".join(done), f"翻译出错：{exc}")

        threading.Thread(target=work, name="desk-translate", daemon=True).start()

    def on_partial(self, job: int, text: str):
        if job != self.job:
            return
        self.output.setPlainText(text)
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def on_done(self, job: int, text: str, note: str):
        if job != self.job:
            return
        self.output.setPlainText(text)
        self.note_label.setText(note)
        self.copy_button.setEnabled(bool(text))

    def copy_output(self):
        QApplication.clipboard().setText(self.output.toPlainText())
        self.note_label.setText("译文已复制")

    # ------------------------------------------------------------------ audio captions
    def _show_audio(self, running: bool):
        pending = self.audio_pending[0] if self.audio_pending else None
        if pending == "opening":
            label, button = "正在打开音频翻译，加载 Whisper…", "正在打开…"
        elif pending == "closing":
            label, button = "正在关闭音频翻译，释放 Whisper…", "正在关闭…"
        elif running:
            label, button = "音频翻译：字幕运行中，Whisper 已加载", "关闭音频翻译"
        else:
            label, button = "音频翻译：未运行，不占用 Whisper 显存", "打开音频翻译"
        self.audio_label.setText(label)
        self.audio_button.setText(button)
        self.audio_button.setEnabled(pending is None)
        primary = "primary" if not running and pending is None else ""
        if self.audio_button.objectName() != primary:
            self.audio_button.setObjectName(primary)
            self.audio_button.style().unpolish(self.audio_button)  # apply the style of the new object name
            self.audio_button.style().polish(self.audio_button)
        self.audio_hint.setText(self.audio_note or ("字幕在独立的音频翻译窗口里，关闭它会释放 Whisper，文本翻译不受影响。" if running else
                                                    "打开后会加载 Whisper 并开始实时中文字幕。"))
        if self.tray:
            self.tray_audio.setText(button)
            self.tray_audio.setEnabled(pending is None)

    def toggle_audio(self):
        if self.audio_pending:
            return
        if backend.Audio.running(self.status):
            self.close_audio()
        else:
            self.open_audio()

    def open_audio(self):
        if self.audio_pending or backend.Audio.running(self.status):
            return
        self.audio_note = ""
        self.audio_pending = ("opening", time.monotonic() + AUDIO_OPEN_SECONDS)
        self._show_audio(False)
        log.info("opening audio captions")
        self._in_background(self.audio.open)

    def close_audio(self):
        if self.audio_pending and self.audio_pending[0] == "closing":
            return
        self.audio_note = ""
        self.audio_pending = ("closing", time.monotonic() + AUDIO_CLOSE_SECONDS)
        self._show_audio(True)
        log.info("closing audio captions")
        self._in_background(self.audio.close)

    def _in_background(self, action):
        def work():
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                log.warning("audio control failed: %s", exc)
                self.bridge.audio_error.emit(str(exc))
            self.poll()

        threading.Thread(target=work, name="desk-audio", daemon=True).start()

    def on_audio_error(self, message: str):
        self.audio_pending = None
        self.audio_note = message
        self._show_audio(backend.Audio.running(self.status))

    # ------------------------------------------------------------------ window
    def handle_command(self, command: bytes):
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
            log.warning("settings not saved: %s", exc)

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
        event.accept()
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((COMMANDS[arg] for arg in argv if arg in COMMANDS), b"show")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("通用翻译")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(False)

    # One desk per user: a second start passes its command to the running one.
    socket = QLocalSocket()
    socket.connectToServer(APP_KEY)
    if socket.waitForConnected(300):
        socket.write(command)
        socket.flush()
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return 0
    if command == b"quit":
        return 0
    if command == b"close-audio":  # no desk running: just ask the audio app to quit
        backend.Audio().close()
        return 0

    setup_logging()
    QLocalServer.removeServer(APP_KEY)
    server = QLocalServer()
    server.listen(APP_KEY)
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
    return app.exec()


def run():
    try:
        code = main()
    except Exception:  # noqa: BLE001
        log.exception("fatal")
        try:
            import traceback

            QMessageBox.critical(None, "通用翻译出错", traceback.format_exc()[-1500:] + f"\n\n日志：{paths.LOG_DIR}")
        except Exception:  # noqa: BLE001
            pass
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    run()
