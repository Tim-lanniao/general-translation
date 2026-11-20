"""The desk window offscreen: text mode at start, translation, and opening and closing audio captions."""
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from translation_desk import app as desk  # noqa: E402
from translation_desk import backend, paths  # noqa: E402

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
    resolved = True

    def __init__(self):
        self.sessions, self.caption_states = 0, {}
        self.state = "unloaded"
        self.calls = []
        self.answer = None  # None: answer every piece; else a function(text) that may raise
        self.reachable = True
        self.started = threading.Event()

    def start(self):
        self.started.set()
        return self.url

    def status(self):
        if not self.reachable:
            return None
        return {"service": "translation-gateway", "caption_sessions": self.sessions, "caption_states": dict(self.caption_states),
                "model": {"label": "Qwen", "state": self.state}}

    def warm(self):
        self.state = "ready"

    def translate(self, text, source="auto", target="zh", *, context="", on_partial=None, call=None):
        self.calls.append((text, source, target, context))
        if self.answer:
            return self.answer(text, call)
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
        wait_until(lambda: not self.window.polling)
        self.window.poll()
        wait_until(lambda: not self.window.polling)

    def translate(self, text):
        self.window.source_edit.setPlainText(text)
        QTest.keyClick(self.window.source_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: self.window.copy_button.isEnabled() or "出错" in self.window.note_label.text())

    def test_starts_in_text_mode_without_audio(self):
        wait_until(lambda: self.window.model_label.text() == "文本翻译就绪 · Qwen")
        self.assertTrue(self.gateway.started.is_set())
        self.assertEqual(self.audio.opened, 0)
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")
        self.assertIn("不占用 Whisper", self.window.audio_label.text())

    def test_translates_paragraph_by_paragraph(self):
        self.window.source_box.setCurrentIndex(self.window.source_box.findData("de"))
        self.translate("Erster Absatz.\n\nZweiter Absatz.")
        self.assertEqual(self.window.output.toPlainText(), "译：Erster Absatz.\n\n译：Zweiter Absatz.")
        self.assertEqual(self.gateway.calls, [("Erster Absatz.", "de", "zh", ""), ("Zweiter Absatz.", "de", "zh", "Erster Absatz.")])
        self.assertIn("用时", self.window.note_label.text())
        self.assertEqual(self.window.note_label.objectName(), "muted")
        self.window.copy_output()
        self.assertEqual(QApplication.clipboard().text(), self.window.output.toPlainText())

    def test_a_paragraph_that_breaks_off_is_marked_in_the_translation(self):
        def answer(text, call):
            if text.startswith("Zweiter"):
                raise backend.DeskError("翻译中断：网关在译文完成前断开了连接", stage="translate", interrupted=True)
            return f"译：{text}", "stop"

        self.gateway.answer = answer
        self.translate("Erster Absatz.\n\nZweiter Absatz.\n\nDritter Absatz.")
        output = self.window.output.toPlainText()
        self.assertEqual(output.split("\n\n")[0], "译：Erster Absatz.")
        self.assertIn("〔第 2 段翻译中断", output)
        self.assertEqual(output.split("\n\n")[2], "译：Dritter Absatz.")  # the next paragraph is still translated
        self.assertIn("第 2 段没有完整译文", self.window.note_label.text())
        self.assertEqual(self.window.note_label.objectName(), "warning")

    def test_an_unreachable_gateway_leaves_the_rest_untranslated(self):
        def answer(text, call):
            raise backend.DeskError("无法连接翻译网关：refused", stage="translate")

        self.gateway.answer = answer
        self.translate("Erster Absatz.\n\nZweiter Absatz.")
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertIn("〔第 1 段起没有翻译", self.window.output.toPlainText())

    def test_a_new_translation_cancels_the_one_still_waiting(self):
        waiting = threading.Event()

        def answer(text, call):
            if text == "erste":
                waiting.set()
                while not call.cancelled:
                    time.sleep(.01)
                raise CancelledError()
            return f"译：{text}", "stop"

        self.gateway.answer = answer
        self.window.source_edit.setPlainText("erste")
        self.window.translate()
        wait_until(waiting.is_set)
        first = self.window.call
        self.translate("zweite")
        self.assertTrue(first.cancelled)
        self.assertEqual(self.window.output.toPlainText(), "译：zweite")

    def test_audio_states_follow_the_caption_session(self):
        self.window.audio_button.click()
        wait_until(lambda: self.audio.opened == 1)
        self.assertEqual(self.window.audio_label.text(), "正在打开音频翻译，加载 Whisper…")
        self.gateway.sessions, self.gateway.caption_states = 1, {"loading": 1}  # session open, Whisper still loading
        self.refresh()
        self.assertEqual(self.window.audio_label.text(), "正在打开音频翻译，加载 Whisper…")
        self.gateway.caption_states = {"running": 1}
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "关闭音频翻译")
        self.assertIn("Whisper 已加载", self.window.audio_label.text())
        self.gateway.reachable = False  # one unanswered status request changes nothing
        self.refresh()
        self.assertIn("字幕运行中", self.window.audio_label.text())
        self.gateway.reachable = True
        self.window.handle_command(b"close-audio")
        wait_until(lambda: self.audio.closed == 1)
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "正在关闭…")  # the session is still there
        self.gateway.sessions, self.gateway.caption_states = 0, {}
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")

    def test_reports_an_audio_app_that_never_starts_captions(self):
        self.window.handle_command(b"open-audio")
        wait_until(lambda: self.audio.opened == 1 and not self.window.audio_busy)
        self.window.audio_pending = ("opening", time.monotonic() - 1)
        self.refresh()
        self.assertEqual(self.window.audio_button.text(), "打开音频翻译")
        self.assertIn("没有开始字幕", self.window.audio_hint.text())


if __name__ == "__main__":
    unittest.main()
