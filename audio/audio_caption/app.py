"""Control window and floating subtitles for desktop audio translation."""
from __future__ import annotations

import html
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime

from . import config

CFG = config.load()
config.prepare_environment(CFG)

from PySide6.QtCore import QDir, QLockFile, QObject, QRect, QRectF, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QTextCursor  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,  # noqa: E402
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSplitter, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from . import capture, translator  # noqa: E402
from .engine import CaptionEngine, gpu_memory  # noqa: E402
from .overlay import CaptionOverlay  # noqa: E402

log = logging.getLogger("audio_caption")
APP_KEY = "TeamsLiveCaption-" + os.environ.get("USERNAME", "user")  # unchanged, so old and new builds stay single-instance

STYLE = """
QWidget#root { background: #F3F6F4; }
QLabel { color: #23332C; }
QLabel#title { font: 700 21px 'Microsoft YaHei UI'; color: #1D4034; }
QLabel#subtitle { color: #6A7D73; font-size: 12px; }
QLabel#status { font: 600 15px 'Microsoft YaHei UI'; }
QLabel#muted { color: #6A7D73; font-size: 12px; }
QLabel#formlabel { color: #4A5C53; }
QFrame#card { background: #FFFFFF; border: 1px solid #DCE5DF; border-radius: 12px; }
QPushButton { font: 600 13px 'Microsoft YaHei UI'; border-radius: 8px; padding: 8px 16px; border: 1px solid #CBD8D0; background: #FFFFFF; color: #23332C; }
QPushButton:hover { background: #EEF4F0; }
QPushButton:disabled { color: #A8B5AE; background: #F5F7F6; border-color: #E1E7E3; }
QPushButton#primary { background: #276856; color: white; border: none; padding: 10px 26px; font-size: 14px; }
QPushButton#primary:hover { background: #2F7C66; }
QPushButton#primary:disabled { background: #9DB8AE; color: #EEF3F0; }
QPushButton#danger { color: #A1442F; border-color: #E5C9C1; }
QPushButton#danger:hover { background: #FBF0ED; }
QPushButton#danger:disabled { color: #CDB9B3; border-color: #EFE6E3; background: #FAFBFA; }
QComboBox, QLineEdit { border: 1px solid #CBD8D0; border-radius: 7px; padding: 6px 9px; background: #FFFFFF; font-size: 13px; min-height: 20px; }
QComboBox:disabled, QLineEdit:disabled { background: #F2F5F3; color: #8C9A93; }
QComboBox:focus, QLineEdit:focus { border-color: #3B8F76; }
QCheckBox { color: #34463D; spacing: 6px; }
QTextBrowser { border: none; background: transparent; }
QTextBrowser#live { background: #FFF8E6; border: 1px solid #F1E2B8; border-radius: 8px; padding: 4px 6px; }
QSplitter#split::handle:vertical { background: transparent; border-top: 1px dashed #C5D3CA; margin: 5px 160px; }
QSplitter#split::handle:vertical:hover { border-top: 2px solid #3B8F76; }
QProgressBar { border: none; background: #E3EBE6; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #3B8F76; border-radius: 2px; }
QToolButton#flat { border: none; color: #276856; font: 600 12px 'Microsoft YaHei UI'; padding: 4px 8px; border-radius: 6px; }
QToolButton#flat:hover { background: #E8F1EC; }
QSlider::groove:horizontal { height: 4px; background: #DCE5DF; border-radius: 2px; }
QSlider::handle:horizontal { background: #276856; width: 14px; margin: -6px 0; border-radius: 7px; }
"""

STATE_COLORS = {"idle": "#7B8A82", "loading": "#B07A1E", "running": "#25845F", "stopping": "#B07A1E", "error": "#B3432E"}


class Bridge(QObject):
    event = Signal(dict)


class LevelMeter(QWidget):
    def __init__(self):
        super().__init__()
        self.level = 0.0
        self.speaking = False
        self.setFixedHeight(12)
        self.setMinimumWidth(160)

    def set(self, rms: float, speaking: bool):
        import math

        db = 20 * math.log10(max(rms, 1e-5))
        self.level = max(0.0, min(1.0, (db + 60) / 54))
        self.speaking = speaking
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#E3EBE6"))
        p.drawRoundedRect(r, 4, 4)
        if self.level > 0:
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, QColor("#7CC6A8"))
            g.setColorAt(1, QColor("#F2B544") if self.speaking else QColor("#3B8F76"))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * self.level, r.height()), 4, 4)
        p.end()


def make_icon() -> QIcon:
    if not config.ICON_PATH.exists():
        img = QImage(256, 256, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 256, 256)
        g.setColorAt(0, QColor("#2F8A70"))
        g.setColorAt(1, QColor("#173A2F"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(8, 8, 240, 240), 56, 56)
        p.fillPath(path, g)
        bubble = QPainterPath()
        bubble.addRoundedRect(QRectF(40, 52, 176, 120), 30, 30)
        bubble.moveTo(84, 168)
        bubble.lineTo(70, 212)
        bubble.lineTo(124, 170)
        p.fillPath(bubble.simplified(), QColor("#FFFFFF"))
        p.setPen(QColor("#F2B544"))
        p.setBrush(QColor("#F2B544"))
        p.drawRoundedRect(QRectF(64, 140, 128, 10), 5, 5)
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(82)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor("#1F5444"))
        p.drawText(QRectF(40, 40, 176, 104), Qt.AlignCenter, "译")
        p.end()
        img.save(str(config.ICON_PATH), "ICO")
    return QIcon(str(config.ICON_PATH))


class ControlWindow(QWidget):
    shutdown_done = Signal()

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setObjectName("root")
        self.setWindowTitle("通用音频翻译 · 实时中文字幕")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLE)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(900, avail.width() - 40), min(980, avail.height() - 40))
        geo = cfg.get("console_geometry")
        if geo and any(sc.availableGeometry().intersects(QRect(*geo)) for sc in QApplication.screens()):
            self.setGeometry(*geo)
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.engine = CaptionEngine(cfg, self.bridge.event.emit)
        self.overlay = CaptionOverlay(cfg)
        self.overlay.setWindowIcon(self.windowIcon())
        self.overlay.console_requested.connect(self.bring_to_front)
        self.overlay.pause_requested.connect(self.toggle_pause)
        self.overlay.settings_changed.connect(self.on_overlay_settings)
        self.live_id = None
        self.history_text: list[str] = []
        self.record_dir = ""
        self.closing = False
        self._build()
        self.refresh_sources()
        self.set_state("idle", "未启动 · 点击「开始字幕」")
        self.gpu_timer = QTimer(self, interval=4000, timeout=self.poll_gpu)
        self.gpu_timer.start()
        self.poll_gpu()
        self.shutdown_done.connect(self._after_shutdown)

    # ------------------------------------------------------------------ layout
    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        return f

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        t = QLabel("通用音频翻译")
        t.setObjectName("title")
        s = QLabel("实时中文字幕 · 本机 Whisper 识别与 Qwen 翻译")
        s.setObjectName("subtitle")
        titles.addWidget(t)
        titles.addWidget(s)
        head.addLayout(titles)
        head.addStretch()
        self.settings_toggle = QToolButton()
        self.settings_toggle.setObjectName("flat")
        self.settings_toggle.setToolTip("收起/展开设置区，让字幕记录区更大")
        self.settings_toggle.clicked.connect(lambda: self.set_settings_visible(not self.settings_card.isVisible()))
        head.addWidget(self.settings_toggle, 0, Qt.AlignBottom)
        root.addLayout(head)

        # status card
        card = self._card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        cl.setSpacing(8)
        row = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("status")
        row.addWidget(self.status, 1)
        self.start_btn = QPushButton("开始字幕")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.on_primary)
        self.stop_btn = QPushButton("停止字幕并释放识别显存")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.stop_engine)
        self.overlay_btn = QPushButton("隐藏字幕条")
        self.overlay_btn.clicked.connect(self.toggle_overlay)
        for b in (self.overlay_btn, self.stop_btn, self.start_btn):
            row.addWidget(b)
        cl.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        cl.addWidget(self.progress)
        self.source_label = QLabel("声音来源：—")
        self.source_label.setObjectName("muted")
        self.source_label.setWordWrap(True)
        cl.addWidget(self.source_label)
        m_row = QHBoxLayout()
        m_row.setSpacing(12)
        vol = QLabel("输入音量")
        vol.setObjectName("muted")
        self.meter = LevelMeter()
        self.speak_label = QLabel("")
        self.speak_label.setObjectName("muted")
        self.speak_label.setFixedWidth(76)
        m_row.addWidget(vol)
        m_row.addWidget(self.meter, 1)
        m_row.addWidget(self.speak_label)
        cl.addLayout(m_row)
        self.metrics = QLabel("")
        self.metrics.setObjectName("muted")
        cl.addWidget(self.metrics)
        root.addWidget(card)

        # settings card (collapsible; folds away while captions run so the caption area gets the room)
        card = self._card()
        self.settings_card = card
        g = QGridLayout(card)
        g.setContentsMargins(18, 14, 18, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        def label(text):
            l = QLabel(text)
            l.setObjectName("formlabel")
            return l

        self.source_box = QComboBox()
        self.source_box.currentIndexChanged.connect(self.on_source_changed)
        refresh = QToolButton()
        refresh.setObjectName("flat")
        refresh.setText("↻ 刷新")
        refresh.setToolTip("重新列出播放设备")
        refresh.clicked.connect(self.refresh_sources)
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_box, 1)
        src_row.addWidget(refresh)
        g.addWidget(label("声音来源"), 0, 0)
        g.addLayout(src_row, 0, 1)

        self.language_box = QComboBox()
        for code, name in config.LANGUAGES.items():
            self.language_box.addItem(name, code)
        self.language_box.setCurrentIndex(max(0, self.language_box.findData(self.cfg["asr_language"])))
        self.language_box.currentIndexChanged.connect(lambda _: self.save_setting("asr_language", self.language_box.currentData()))
        g.addWidget(label("原文语言"), 1, 0)
        g.addWidget(self.language_box, 1, 1)

        self.topic = QLineEdit(self.cfg.get("topic", ""))
        self.topic.setPlaceholderText("例如：模具项目周会 / 供应商技术评审（让翻译理解场景，可随时修改）")
        self.topic.editingFinished.connect(lambda: self.save_setting("topic", self.topic.text().strip()))
        g.addWidget(label("内容主题"), 2, 0)
        g.addWidget(self.topic, 2, 1)

        self.terms = QPushButton("识别热词…")
        self.terms.clicked.connect(lambda: self.edit_terms(shared=False))
        self.shared_terms = QPushButton("共享翻译术语…")
        self.shared_terms.clicked.connect(lambda: self.edit_terms(shared=True))
        terms_row = QHBoxLayout()
        terms_row.addWidget(self.terms)
        terms_row.addWidget(self.shared_terms)
        g.addWidget(label("术语"), 3, 0)
        g.addLayout(terms_row, 3, 1)

        look = QHBoxLayout()
        look.setSpacing(10)
        look.addWidget(QLabel("字号"))
        self.font_slider = QSlider(Qt.Horizontal)
        self.font_slider.setRange(16, 56)
        self.font_slider.setValue(self.cfg["font_px"])
        self.font_slider.setFixedWidth(150)
        self.font_slider.valueChanged.connect(lambda v: self.overlay.set_font(v))
        look.addWidget(self.font_slider)
        look.addSpacing(16)
        look.addWidget(QLabel("背景不透明度"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(int(self.cfg["overlay_opacity"] * 100))
        self.opacity_slider.setFixedWidth(150)
        self.opacity_slider.valueChanged.connect(self.on_opacity)
        look.addWidget(self.opacity_slider)
        look.addStretch()
        g.addWidget(label("字幕条外观"), 4, 0)
        g.addLayout(look, 4, 1)

        checks = QGridLayout()
        checks.setHorizontalSpacing(24)
        checks.setVerticalSpacing(8)
        self.auto_start = QCheckBox("打开程序后自动开始")
        self.auto_start.setChecked(self.cfg.get("auto_start", True))
        self.auto_start.toggled.connect(lambda v: self.save_setting("auto_start", v))
        self.show_de = QCheckBox("字幕条显示原文")
        self.show_de.setChecked(self.cfg.get("show_german", True))
        self.show_de.toggled.connect(self.overlay.set_show_german)
        self.click_through = QCheckBox("字幕条鼠标穿透")
        self.click_through.setToolTip("开启后，鼠标点击会穿过字幕条落到下方的窗口；关闭后才能拖动字幕条")
        self.click_through.setChecked(self.cfg.get("click_through", False))
        self.click_through.toggled.connect(self.overlay.set_click_through)
        self.hide_capture = QCheckBox("共享屏幕时对方看不到字幕条")
        self.hide_capture.setToolTip("字幕条只显示在你自己的屏幕上，共享屏幕或录屏时会被排除")
        self.hide_capture.setChecked(self.cfg.get("hide_from_capture", True))
        self.hide_capture.toggled.connect(self.overlay.set_hide_from_capture)
        self.auto_fold = QCheckBox("字幕开始后自动收起设置")
        self.auto_fold.setToolTip("运行时收起这块设置，让下方的字幕记录区更大；随时可点右上角“展开设置”")
        self.auto_fold.setChecked(self.cfg.get("auto_collapse_settings", True))
        self.auto_fold.toggled.connect(lambda v: self.save_setting("auto_collapse_settings", v))
        for i, c in enumerate((self.auto_start, self.show_de, self.hide_capture, self.click_through, self.auto_fold)):
            checks.addWidget(c, i // 2, i % 2)
        checks.setColumnStretch(2, 1)
        g.addLayout(checks, 5, 1)
        g.setColumnStretch(1, 1)

        # history card: confirmed captions on top, the live (yellow) caption in its own pane below.
        card = self._card()
        hl = QVBoxLayout(card)
        hl.setContentsMargins(18, 12, 18, 12)
        hl.setSpacing(6)
        hrow = QHBoxLayout()
        ht = QLabel("字幕记录")
        ht.setStyleSheet("font: 600 14px 'Microsoft YaHei UI';")
        hrow.addWidget(ht)
        hint = QLabel("最新定稿在下方，拖动分隔线调整高度")
        hint.setObjectName("muted")
        hrow.addWidget(hint)
        hrow.addStretch()
        smaller = QToolButton()
        smaller.setObjectName("flat")
        smaller.setText("A−")
        smaller.setToolTip("缩小字幕记录字号")
        smaller.clicked.connect(lambda: self.set_history_font(self.cfg.get("history_font_px", 16) - 1))
        bigger = QToolButton()
        bigger.setObjectName("flat")
        bigger.setText("A+")
        bigger.setToolTip("放大字幕记录字号")
        bigger.clicked.connect(lambda: self.set_history_font(self.cfg.get("history_font_px", 16) + 1))
        copy_btn = QToolButton()
        copy_btn.setObjectName("flat")
        copy_btn.setText("复制全部")
        copy_btn.clicked.connect(self.copy_history)
        folder_btn = QToolButton()
        folder_btn.setObjectName("flat")
        folder_btn.setText("打开字幕记录文件夹")
        folder_btn.clicked.connect(self.open_records)
        for b in (smaller, bigger, copy_btn, folder_btn):
            hrow.addWidget(b)
        hl.addLayout(hrow)

        self.history = QTextBrowser()
        self.history.setOpenLinks(False)
        self.live = QTextBrowser()
        self.live.setObjectName("live")
        self.live.setOpenLinks(False)
        self.live.setMinimumHeight(52)
        self.hist_split = QSplitter(Qt.Vertical)
        self.hist_split.setObjectName("split")
        self.hist_split.setHandleWidth(12)
        self.hist_split.setChildrenCollapsible(False)
        self.hist_split.addWidget(self.history)
        self.hist_split.addWidget(self.live)
        self.hist_split.setStretchFactor(0, 1)
        self.hist_split.setStretchFactor(1, 0)
        self.hist_split.setSizes([480, int(self.cfg.get("live_box_px", 110))])
        self.hist_split.splitterMoved.connect(lambda *_: self.save_setting("live_box_px", self.hist_split.sizes()[1]))
        hl.addWidget(self.hist_split, 1)
        self._stick = True
        sb = self.history.verticalScrollBar()
        sb.valueChanged.connect(lambda v: setattr(self, "_stick", v >= sb.maximum() - 4))
        sb.rangeChanged.connect(lambda lo, hi: sb.setValue(hi) if self._stick else None)

        self.main_split = QSplitter(Qt.Vertical)
        self.main_split.setObjectName("split")
        self.main_split.setHandleWidth(14)
        self.main_split.addWidget(self.settings_card)
        self.main_split.addWidget(card)
        self.main_split.setCollapsible(0, True)
        self.main_split.setCollapsible(1, False)
        self.main_split.setStretchFactor(0, 0)
        self.main_split.setStretchFactor(1, 1)
        root.addWidget(self.main_split, 1)

        foot = QLabel("黄色区域显示正在翻译的语句。停顿后，定稿会加入上方记录。拖动分隔线可调整两个区域的高度。")
        foot.setObjectName("muted")
        foot.setWordWrap(True)
        root.addWidget(foot)
        self.set_history_font(self.cfg.get("history_font_px", 16), save=False)
        self.set_live(None)
        self.set_settings_visible(not self.cfg.get("settings_collapsed", False), save=False)

    def set_settings_visible(self, visible: bool, save: bool = True):
        self.settings_card.setVisible(visible)
        if visible and self.main_split.sizes()[0] == 0:
            h = self.settings_card.sizeHint().height()
            self.main_split.setSizes([h, max(200, self.main_split.height() - h)])
        self.settings_toggle.setText("收起设置 ▴" if visible else "展开设置 ▾")
        if save:
            self.save_setting("settings_collapsed", not visible)

    def set_history_font(self, px: int, save: bool = True):
        px = max(12, min(28, px))
        self.cfg["history_font_px"] = px
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(px)
        self.history.document().setDefaultFont(f)
        self.live.document().setDefaultFont(f)
        if save:
            config.save(self.cfg)

    # ------------------------------------------------------------------ settings
    def save_setting(self, key, value):
        self.cfg[key] = value
        config.save(self.cfg)

    def edit_terms(self, *, shared):
        dialog = QDialog(self)
        dialog.setWindowTitle("共享翻译术语" if shared else "字幕识别热词")
        dialog.resize(580, 440)
        layout = QVBoxLayout(dialog)
        help_text = QLabel("每行填写 原文=译法。字幕和文本翻译都使用这里的词对，每句只加入匹配的条目。"
                           if shared else "每行填写一个德语人名或术语。用 [展会] 等标题分组，内容主题包含组名时优先识别。")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        if shared:  # the gateway owns the shared glossary
            try:
                current = translator.read_shared_terms()["terms"]
            except (OSError, RuntimeError) as exc:
                QMessageBox.warning(self, "共享翻译术语", f"无法连接翻译网关：{exc}")
                return
        else:
            current = self.cfg.get("terms", "")
        editor = QPlainTextEdit(current)
        editor.setPlaceholderText("Frau Schäfer=舍费尔女士\n[展会]\nSpritzguss=注塑\nWerkzeugabnahme=模具验收")
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            raw = editor.toPlainText().strip()
            if shared:
                try:
                    saved = translator.save_shared_terms(raw)
                except (OSError, RuntimeError) as exc:
                    QMessageBox.warning(self, "共享翻译术语", f"术语没有保存：{exc}")
                    return
                if self.engine.translator:  # retranslate the current sentence with the new terms
                    self.engine.translator.terms_revision = saved["revision"]
            else:
                self.save_setting("terms", raw)

    def on_overlay_settings(self, changes: dict):
        for k, v in changes.items():
            self.cfg[k] = v
        if "font_px" in changes and self.font_slider.value() != changes["font_px"]:
            self.font_slider.blockSignals(True)
            self.font_slider.setValue(changes["font_px"])
            self.font_slider.blockSignals(False)
        if "show_german" in changes and self.show_de.isChecked() != changes["show_german"]:
            self.show_de.blockSignals(True)
            self.show_de.setChecked(changes["show_german"])
            self.show_de.blockSignals(False)
        config.save(self.cfg)

    def on_opacity(self, v: int):
        self.overlay.set_opacity(v / 100)
        self.save_setting("overlay_opacity", v / 100)

    def refresh_sources(self):
        current = self.cfg.get("source", "system")
        self.source_box.blockSignals(True)
        self.source_box.clear()
        self.source_box.addItem("全电脑声音（所有播放设备）", "system")
        teams_pid = capture.find_root_process(list(config.APP_SOURCES["auto"][1]))
        found = "，已检测到 Teams" if teams_pid else "，暂未检测到 Teams（先用默认播放设备）"
        self.source_box.addItem(f"Teams 程序{found}", "auto")
        self.source_box.addItem("只听 Edge 浏览器的声音", "edge")
        self.source_box.addItem("只听 Chrome 浏览器的声音", "chrome")
        self.source_box.insertSeparator(self.source_box.count())
        self.source_box.addItem("播放设备：仅系统默认播放设备", "device:default")
        try:
            for d in capture.list_render_devices():
                self.source_box.addItem(f"播放设备：{d.name}{'（当前默认）' if d.default else ''}", "device:" + d.id)
        except Exception as exc:
            log.warning("device list failed: %s", exc)
        sample = self.cfg.get("sample_wav") or ""
        if sample and os.path.isfile(sample):
            self.source_box.insertSeparator(self.source_box.count())
            self.source_box.addItem("测试：回放一段德语样例音频（检查字幕是否正常，不发声）", "replay:" + sample)
        idx = self.source_box.findData(current)
        self.source_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.source_box.blockSignals(False)

    def on_source_changed(self, _):
        key = self.source_box.currentData()
        if key:
            self.save_setting("source", key)
            self.engine.set_source(key)

    # ------------------------------------------------------------------ actions
    def on_primary(self):
        st = self.engine.state
        if st in ("idle", "error"):
            self.start_engine()
        elif st == "running":
            self.toggle_pause()

    def start_engine(self):
        self.cfg["topic"] = self.topic.text().strip()
        config.save(self.cfg)
        self.overlay.clear()
        self.overlay.set_message("正在加载模型，请稍候…")
        if not self.overlay.isVisible():
            self.toggle_overlay()
        self.engine.start_async()
        self.set_state("loading", "正在准备…")

    def start_captions(self, gateway_url=""):
        if self.closing or self.engine.state == "stopping":
            return False
        if gateway_url:
            if self.engine.state in ("idle", "error"):
                os.environ["TRANSLATION_GATEWAY_URL"] = gateway_url
            else:
                session = self.engine.translator
                cfg = session.gateway_cfg if session is not None else translator.gateway_settings()
                if gateway_url != translator.client.gateway_url(cfg):
                    return False  # keep an existing live session on its own gateway
        if self.engine.state in ("idle", "error"):
            self.start_engine()
        elif self.engine.state == "running" and self.engine.paused:
            self.toggle_pause()
        return True

    def toggle_pause(self):
        if self.engine.state != "running":
            return
        threading.Thread(target=self.engine.resume if self.engine.paused else self.engine.pause, daemon=True).start()

    def stop_engine(self):
        if self.engine.state in ("idle", "error"):
            return
        self.set_state("stopping", "正在停止字幕并释放识别显存…")
        threading.Thread(target=self.engine.shutdown, daemon=True).start()

    def toggle_overlay(self):
        if self.overlay.isVisible():
            self.overlay.hide()
        else:
            self.overlay.show()
            self.overlay.raise_()
        self.overlay_btn.setText("隐藏字幕条" if self.overlay.isVisible() else "显示字幕条")

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def copy_history(self):
        QApplication.clipboard().setText("\n\n".join(self.history_text))
        self.metrics.setText("已复制全部字幕记录到剪贴板")

    def open_records(self):
        os.startfile(self.record_dir or str(config.RECORD_DIR))

    def poll_gpu(self):
        def work():
            used, total = gpu_memory()
            self.bridge.event.emit({"type": "gpu", "used": used, "total": total})

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ events
    def set_state(self, state: str, text: str):
        color = STATE_COLORS.get(state, "#23332C")
        if state == "running" and self.engine.paused:
            color = "#7B8A82"
        self.status.setText(f"<span style='color:{color}'>●</span>&nbsp; {html.escape(text)}")
        self.progress.setVisible(state in ("loading", "stopping"))
        loaded = state in ("running",)
        self.start_btn.setEnabled(state in ("idle", "error", "running"))
        self.start_btn.setText({"idle": "开始字幕", "error": "重新开始", "loading": "加载中…", "stopping": "停止中…"}.get(state, "继续字幕" if self.engine.paused else "暂停字幕"))
        self.stop_btn.setEnabled(state in ("loading", "running"))
        self.language_box.setEnabled(state in ("idle", "error"))
        if not loaded:
            self.meter.set(0, False)
            self.speak_label.setText("")

    def on_event(self, ev: dict):
        t = ev["type"]
        if t == "level":
            self.meter.set(ev["rms"], ev["speaking"])
            self.speak_label.setText("有人说话" if ev["speaking"] else "")
            self.overlay.set_speaking(ev["speaking"])
        elif t == "caption":
            # A revision often starts with the exact text already on screen.
            # Leave those words readable until the first actual correction arrives.
            if (ev.get("streaming") and ev["id"] == self.overlay.cur_id
                    and self.overlay.cur_zh.startswith(ev["zh"])):
                if self.overlay.cur_de == ev["de"]:
                    return
                ev = dict(ev, zh=self.overlay.cur_zh)
            self.overlay.show_caption(ev)
            self.on_caption(ev)
        elif t == "drop":
            self.overlay.drop_caption(ev["id"])
            if ev["id"] == self.live_id:
                self.set_live(None)
        elif t == "status":
            self.set_state(ev["state"], ev["text"])
            self.overlay.set_phase(ev["state"])
            if ev["state"] == "running" and not self.engine.paused:
                self.overlay.set_message("字幕已就绪，等待发言…")
                if self.cfg.get("auto_collapse_settings", True) and self.settings_card.isVisible():
                    self.set_settings_visible(False, save=False)
            elif ev["state"] == "loading":
                self.overlay.set_message(ev["text"])
            elif ev["state"] == "error":
                self.overlay.set_message("字幕启动失败，请看控制台提示")
                self.bring_to_front()
                QMessageBox.warning(self, "无法开始字幕", ev["text"])
            elif ev["state"] == "idle":
                self.set_live(None)
                if self.cfg.get("auto_collapse_settings", True) and not self.closing:
                    self.set_settings_visible(True, save=False)
                self.overlay.clear()
                self.overlay.set_message(ev["text"])
        elif t == "paused":
            self.overlay.set_paused(ev["paused"])
            self.set_state(self.engine.state, "已暂停（模型仍在显存中，可随时继续）" if ev["paused"] else "字幕运行中")
        elif t == "source":
            self.source_label.setText(("" if ev["ok"] else "⚠ ") + ev["text"])
        elif t == "metrics":
            self.record_dir = ev.get("record_dir") or self.record_dir
            self._metrics = ev
            self._render_metrics()
        elif t == "gpu":
            self._gpu = ev
            self._render_metrics()
        elif t == "warning":
            self.source_label.setText("⚠ " + ev["text"])
        elif t == "idle_release":
            self.stop_engine()
            self.overlay.set_message(f"超过 {ev['minutes']} 分钟没有人说话，已自动停止字幕并释放识别显存")

    def _render_metrics(self):
        parts = []
        m = getattr(self, "_metrics", None)
        if m:
            parts.append(f"定稿 {m['count']} 句")
            if m.get("prov_lag_median") is not None:
                parts.append(f"临时译文完成中位 {m['prov_lag_median']:.1f} 秒")
            if m.get("lag_median") is not None:
                parts.append(f"停顿后定稿约 {m['lag_median']:.1f} 秒")
        gpu = getattr(self, "_gpu", None)
        if gpu and gpu.get("total"):
            parts.append(f"显存 {gpu['used']/1024:.1f} / {gpu['total']/1024:.1f} GB")
        self.metrics.setText(" · ".join(parts))

    def set_live(self, ev: dict | None):
        if not ev:
            self.live_id = None
            self.live.setHtml("<span style='color:#A89A72'>正在说的内容会显示在这里。停顿后，定稿会加入上方记录。</span>")
            return
        self.live_id = ev["id"]
        zh = ev.get("zh")
        self.live.setHtml(f"<span style='color:#9A7B22;font-weight:600'>正在说 · </span>"
                          f"<span style='color:#4A3B12'>{html.escape(zh) if zh else '<i>翻译中…</i>'}</span><br>"
                          f"<span style='color:#9A8A5E;font-size:small'>{html.escape(ev['de'])}</span>")
        lsb = self.live.verticalScrollBar()
        lsb.setValue(lsb.maximum())

    def on_caption(self, ev: dict):
        zh = ev.get("zh")
        if ev["state"] != "final":
            self.set_live(ev)
            return
        stamp = datetime.fromtimestamp(ev["wall_time"]).strftime("%H:%M:%S")
        block = (f"<p style='margin:0 0 12px 0'><span style='color:#8A9A91;font-size:small'>{stamp}</span>&nbsp;&nbsp;"
                 f"<span style='color:#1E2F28'>{html.escape(zh or '')}</span><br>"
                 f"<span style='color:#7C8C84;font-size:small'>{html.escape(ev['de'])}</span></p>")
        cursor = QTextCursor(self.history.document())
        cursor.movePosition(QTextCursor.End)
        if not self.history.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(block)
        self.history_text.append(f"[{stamp}] {zh}\n{ev['de']}")
        if ev["id"] == self.live_id:
            self.set_live(None)

    # ------------------------------------------------------------------ close
    def quit_now(self):
        self.skip_confirm = True
        self.close()

    def closeEvent(self, e):
        if self.closing:
            e.accept()
            return
        if self.engine.state in ("running", "loading") and not getattr(self, "skip_confirm", False):
            ans = QMessageBox.question(self, "退出通用音频翻译", "退出会释放识别显存，翻译模型由网关按需卸载。确定退出吗？")
            if ans != QMessageBox.Yes:
                e.ignore()
                return
        e.ignore()
        self.closing = True
        self.overlay.hide()
        self.set_state("stopping", "正在退出并释放识别显存…")
        self.setEnabled(False)

        def work():
            try:
                self.engine.shutdown()
            finally:
                self.shutdown_done.emit()

        threading.Thread(target=work, daemon=True).start()

    def _after_shutdown(self):
        g = self.normalGeometry() if self.isMaximized() else self.geometry()
        self.cfg["console_geometry"] = [g.x(), g.y(), g.width(), g.height()]
        config.save(self.cfg)
        QApplication.quit()


def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(config.LOG_DIR / "audio-caption.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def forward_to_audio(command):
    """None if there is no listener, otherwise the exit status for the forwarding process."""
    sock = QLocalSocket()
    sock.connectToServer(APP_KEY)
    if not sock.waitForConnected(300):
        return None
    sock.write(command)
    sock.flush()
    if sock.bytesToWrite() and not sock.waitForBytesWritten(1000):
        return 1
    if command == b"quit":
        # The resident holds this socket until its engine has stopped and its listener is closed.
        disconnected = QLocalSocket.LocalSocketState.UnconnectedState
        if sock.state() != disconnected and not sock.waitForDisconnected(55000) and sock.state() != disconnected:
            return 1
    if command.startswith(b"start "):
        if not sock.bytesAvailable() and not sock.waitForReadyRead(3000):
            return 2  # an older resident cannot confirm the selected gateway
        return 0 if bytes(sock.readAll()) == b"ok" else 2
    return 0


def main():
    setup_logging()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("通用音频翻译")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # single instance: a second launch passes its command (show, start captions, quit) to the running one
    command = b"quit" if "--quit" in sys.argv else b"start" if "--start" in sys.argv else b"show"
    requested_url = os.environ.get("TRANSLATION_GATEWAY_URL", "").rstrip("/")
    message = b"start " + requested_url.encode("utf-8") if command == b"start" and requested_url else command
    lock = QLockFile(os.path.join(QDir.tempPath(), APP_KEY + ".lock"))
    lock.setStaleLockTime(0)
    deadline = time.monotonic() + 10
    while True:
        owned = lock.tryLock(0)
        # An older resident has a listener but no lock. A new one holds its lock from
        # before listen() until after shutdown and closing the command server.
        forwarded = forward_to_audio(message)
        if forwarded is not None:
            return forwarded
        if owned:
            if command == b"quit":
                return 0
            break
        if time.monotonic() >= deadline:
            return 1
        time.sleep(.15)
    QLocalServer.removeServer(APP_KEY)
    server = QLocalServer()
    if not server.listen(APP_KEY):
        raise RuntimeError(f"无法建立字幕命令通道：{server.errorString()}")

    cfg = CFG
    win = ControlWindow(cfg)
    quit_waiters = []

    def on_connection():
        conn = server.nextPendingConnection()
        msg = bytes(conn.readAll()) if conn.bytesAvailable() or conn.waitForReadyRead(500) else b""
        if msg == b"quit":
            quit_waiters.append(conn)
            if not win.closing:  # a repeated quit must not cut the shutdown short (the session would linger)
                win.quit_now()
            return
        if not win.closing:
            win.bring_to_front()
            win.overlay.show()
        action, _, address = msg.partition(b" ")
        if action == b"start":
            started = win.start_captions(address.decode("utf-8"))
            if address:
                conn.write(b"ok" if started else b"unavailable")
                conn.flush()
        conn.disconnectFromServer()
        conn.deleteLater()

    server.newConnection.connect(on_connection)
    win.show()
    win.overlay.show()
    if command == b"start" or (cfg.get("auto_start", True) and "--no-auto-start" not in sys.argv):
        QTimer.singleShot(400, win.start_engine)
    snap = os.environ.get("TEAMS_CAPTION_SNAPSHOT")
    if snap:  # debugging aid: periodic screenshots of both windows
        counter = iter(range(1000))

        def grab():
            n = next(counter)
            win.grab().save(os.path.join(snap, f"console-{n:03d}.png"))
            if win.overlay.isVisible():
                win.overlay.grab().save(os.path.join(snap, f"overlay-{n:03d}.png"))

        t = QTimer(win, interval=2500, timeout=grab)
        t.start()
    log.info("UI started")
    code = app.exec()
    server.close()
    lock.unlock()
    for conn in quit_waiters:
        conn.disconnectFromServer()
    return code


def run():
    try:
        code = main()
    except Exception:
        logging.getLogger("audio_caption").exception("fatal")
        try:
            import traceback

            QMessageBox.critical(None, "通用音频翻译出错", traceback.format_exc()[-1500:] + f"\n\n日志：{config.LOG_DIR}")
        except Exception:
            pass
        code = 1
    os._exit(code)  # daemon threads hold CUDA handles; exit promptly after cleanup


if __name__ == "__main__":
    run()
