"""The desk window offscreen: text mode at start, translation, and opening and closing audio captions."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from translation_desk import app as desk  # noqa: E402
from translation_desk import paths  # noqa: E402

APP = QApplication.instance() or QApplication([])


def wait_until(condition, seconds=5):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(.01)
    QApplication.processEvents()
    if not condition():
        raise AssertionError("timed out waiting for the window")


class FakeGateway:
    url = "http://127.0.0.1:18083"

    def __init__(self):
        self.sessions = 0
        self.state = "unloaded"
        self.calls = []
        self.started = threading.Event()

    def start(self):
        self.started.set()
        return self.url

    def status(self):
        return {"service": "translation-gateway", "caption_sessions": self.sessions,
                "model": {"label": "Qwen", "state": self.state}}

    def warm(self):
        self.state = "ready"

    def translate(self, text, source="auto", target="zh", *, context="", on_partial=None, cancelled=None):
        self.calls.append((text, source, target, context))
        on_partial("半句")
        return f"译：{text}", "stop"


class FakeAudio:
    def __init__(self):
        self.opened = self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1


class DeskWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for key, value in (("DATA_DIR", Path(tmp.name)), ("SETTINGS_PATH", Path(tmp.name) / "desk.json")):
            p = patch.object(paths, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.gateway, self.audio = FakeGateway(), FakeAudio()
        self.window = desk.DeskWindow(self.gateway, self.audio, dict(paths.DEFAULTS))
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.poll_timer.stop)

    def refresh(self):
        self.window.poll()
        wait_until(lambda: not self.window.polling)

    def test_starts_in_text_mode_without_audio(self):
        wait_until(lambda: self.window.model_label.text() == "文本翻译就绪 · Qwen")
        self.assertTrue(self.gateway.started.is_set())
        self.assertEqual(self.audio.opened, 0)
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")
        self.assertIn("不占用 Whisper", self.window.audio_label.text())

    def test_translates_paragraph_by_paragraph(self):
        self.window.source_box.setCurrentIndex(self.window.source_box.findData("de"))
        self.window.source_edit.setPlainText("Erster Absatz.\n\nZweiter Absatz.")
        QTest.keyClick(self.window.source_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: self.window.copy_button.isEnabled())
        self.assertEqual(self.window.output.toPlainText(), "译：Erster Absatz.\n\n译：Zweiter Absatz.")
        self.assertEqual(self.gateway.calls, [("Erster Absatz.", "de", "zh", ""), ("Zweiter Absatz.", "de", "zh", "Erster Absatz.")])
        self.assertIn("用时", self.window.note_label.text())
        self.window.copy_output()
        self.assertEqual(QApplication.clipboard().text(), self.window.output.toPlainText())

    def test_opens_and_closes_audio_captions_on_request(self):
        self.window.audio_button.click()
        wait_until(lambda: self.audio.opened == 1)
        self.assertEqual(self.window.audio_button.text(), "正在打开…")
        self.assertFalse(self.window.audio_button.isEnabled())
        self.gateway.sessions = 1  # the audio app opened its caption session
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "关闭音频翻译")
        self.assertIn("Whisper 已加载", self.window.audio_label.text())
        self.window.handle_command(b"close-audio")
        wait_until(lambda: self.audio.closed == 1)
        self.gateway.sessions = 0
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")

    def test_reports_an_audio_app_that_never_starts_captions(self):
        self.window.handle_command(b"open-audio")
        wait_until(lambda: self.audio.opened == 1)
        self.window.audio_pending = ("opening", time.monotonic() - 1)
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")
        self.assertIn("开始字幕", self.window.audio_hint.text())


if __name__ == "__main__":
    unittest.main()
