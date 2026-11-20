"""Exercise the real Qt command channel across processes with a simulated caption engine."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


def run_test_window():
    from types import SimpleNamespace
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from audio_caption import app

    folder = Path(os.environ["TRANSLATION_DATA"])

    class Window(QWidget):
        start_captions = app.ControlWindow.start_captions

        def __init__(self, cfg):
            super().__init__()
            self.overlay = QWidget()
            self.closing = False
            self.engine = SimpleNamespace(state="running", paused=True, translator=None)
            QTimer.singleShot(0, lambda: (folder / "ready").touch())

        def bring_to_front(self):
            pass

        def toggle_pause(self):
            self.engine.paused = False
            (folder / "resumed").touch()

        def quit_now(self):
            self.closing = True
            QTimer.singleShot(600, self.finish_shutdown)

        def finish_shutdown(self):
            (folder / "stopped").touch()
            QApplication.quit()

    app.ControlWindow = Window
    return app.main()


class CommandTests(unittest.TestCase):
    def test_forwarded_start_resumes_and_quit_waits_for_external_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            env = dict(os.environ, USERNAME="caption-test-" + uuid4().hex, QT_QPA_PLATFORM="offscreen",
                       TRANSLATION_DATA=tmp, TRANSLATION_GATEWAY_URL="http://127.0.0.1:18400",
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            cmd = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--test-window"]
            resident = subprocess.Popen([*cmd, "--no-auto-start"], env=env, stdout=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while not (folder / "ready").exists():
                    self.assertIsNone(resident.poll(), "resident exited before listening")
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                subprocess.run([*cmd, "--start"], env=env, timeout=12, check=True)
                self.assertTrue((folder / "resumed").exists())
                other = dict(env, TRANSLATION_GATEWAY_URL="http://127.0.0.1:18401")
                self.assertEqual(subprocess.run([*cmd, "--start"], env=other, timeout=12).returncode, 2)
                subprocess.run([*cmd, "--quit"], env=env, timeout=15, check=True)
                self.assertTrue((folder / "stopped").exists(), "quit returned before shutdown finished")
                self.assertEqual(resident.wait(5), 0)
            finally:
                if resident.poll() is None:
                    resident.kill()
                resident.wait()


if __name__ == "__main__":
    if "--test-window" in sys.argv:
        sys.exit(run_test_window())
    unittest.main()
