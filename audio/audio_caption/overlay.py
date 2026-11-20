"""Always-on-top translucent subtitle bar."""
from __future__ import annotations

import ctypes
import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QTextLayout, QTextOption
from PySide6.QtWidgets import QApplication, QHBoxLayout, QToolButton, QWidget

from .config import LANGUAGES

PROVISIONAL = QColor("#F2B544")
FINAL = QColor("#39D3A0")
IDLE = QColor("#8792A2")
ZH_FONT = "Microsoft YaHei UI"
DE_FONT = "Segoe UI"


def wrap_lines(text: str, font: QFont, width: float) -> list[str]:
    layout = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(opt)
    layout.beginLayout()
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append(text[line.textStart() : line.textStart() + line.textLength()].strip())
    layout.endLayout()
    return [l for l in lines if l]


def tail_lines(text: str, font: QFont, width: float, n: int) -> list[str]:
    lines = wrap_lines(text, font, width)
    if len(lines) <= n:
        return lines
    keep = lines[-n:]
    fm = QFontMetrics(font)
    keep[0] = fm.elidedText("…" + keep[0], Qt.ElideRight, int(width))
    return keep


class CaptionOverlay(QWidget):
    console_requested = Signal()
    pause_requested = Signal()
    settings_changed = Signal(dict)

    EDGE = 10

    def __init__(self, cfg: dict):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("通用音频翻译 · 字幕条")
        self.cfg = cfg
        self.prev_zh = ""
        self.cur_id = None
        self.cur_zh = ""
        self.cur_de = ""
        self.cur_state = "idle"  # provisional | final | idle
        self.message = "点击控制台的「开始字幕」"
        self.speaking = False
        self.paused = False
        self.phase = "idle"
        self.last_update = time.time()
        self._drag: QPoint | None = None
        self._resize: str | None = None
        self._geom0 = None

        self.bar = QWidget(self)
        lay = QHBoxLayout(self.bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.buttons = {}
        for key, text, tip in (("smaller", "A−", "缩小字号（也可 Ctrl+滚轮）"), ("bigger", "A+", "放大字号"), ("german", "原", "显示/隐藏原文"),
                               ("pause", "⏸", "暂停/继续字幕"), ("console", "☰", "打开控制台"), ("hide", "✕", "隐藏字幕条（控制台可再次显示）")):
            b = QToolButton(self.bar)
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(30, 24)
            b.setStyleSheet("QToolButton{color:#E8EDF4;background:rgba(255,255,255,0.10);border:none;border-radius:6px;font:600 12px 'Microsoft YaHei UI';}"
                            "QToolButton:hover{background:rgba(255,255,255,0.24);} QToolButton:checked{background:rgba(57,211,160,0.40);}")
            lay.addWidget(b)
            self.buttons[key] = b
        self.buttons["german"].setCheckable(True)
        self.buttons["german"].setChecked(cfg.get("show_german", True))
        self.buttons["smaller"].clicked.connect(lambda: self.set_font(self.cfg["font_px"] - 2))
        self.buttons["bigger"].clicked.connect(lambda: self.set_font(self.cfg["font_px"] + 2))
        self.buttons["german"].toggled.connect(self._toggle_german)
        self.buttons["pause"].clicked.connect(self.pause_requested.emit)
        self.buttons["console"].clicked.connect(self.console_requested.emit)
        self.buttons["hide"].clicked.connect(self.hide)
        self.bar.hide()

        self._hover_timer = QTimer(self, interval=600, timeout=self._check_hover)
        self._hover_timer.start()
        self._tick = QTimer(self, interval=1000, timeout=self.update)
        self._tick.start()
        self._apply_geometry()
        self.set_click_through(cfg.get("click_through", False), save=False)

    # ---------------------------------------------------------------- geometry
    def _fonts(self):
        px = self.cfg["font_px"]
        zh = QFont(ZH_FONT)
        zh.setPixelSize(px)
        zh.setWeight(QFont.DemiBold)
        prev = QFont(ZH_FONT)
        prev.setPixelSize(max(12, round(px * 0.62)))
        de = QFont(DE_FONT)
        de.setPixelSize(max(12, round(px * 0.56)))
        small = QFont(ZH_FONT)
        small.setPixelSize(12)
        return zh, prev, de, small

    MAX_LINES = 6

    def _heights(self, n: int | None = None):
        zh, prev, de, _ = self._fonts()
        lzh, lprev, lde = (QFontMetrics(f).lineSpacing() for f in (zh, prev, de))
        n = n if n is not None else self.cfg.get("main_lines", 2)
        h = 16 + 26 + lprev + 6 + n * lzh + (6 + 2 * lde if self.cfg.get("show_german", True) else 0) + 18
        return h

    def _lines_for_height(self, h: int) -> int:
        lzh = QFontMetrics(self._fonts()[0]).lineSpacing()
        base = self._heights(0)
        return max(1, min(self.MAX_LINES, round((h - base) / lzh)))

    def _apply_geometry(self):
        screen = QApplication.primaryScreen().availableGeometry()
        g = self.cfg.get("overlay_geometry")
        h = self._heights()
        if g:
            x, y, w = g
            rect = QRect(x, y, w, h)
            if not any(s.availableGeometry().intersects(rect) for s in QApplication.screens()):
                g = None
        if not g:
            w = min(1240, int(screen.width() * 0.64))
            x = screen.x() + (screen.width() - w) // 2
            y = screen.bottom() - h - 36
        self.setGeometry(x, y, w, h)
        self._place_bar()

    def _place_bar(self):
        self.bar.adjustSize()
        self.bar.move(self.width() - self.bar.width() - 14, 10)

    def resizeEvent(self, e):
        self._place_bar()
        super().resizeEvent(e)

    def _save_geometry(self):
        self.cfg["overlay_geometry"] = [self.x(), self.y(), self.width()]
        self.settings_changed.emit({"overlay_geometry": self.cfg["overlay_geometry"]})

    def set_font(self, px: int):
        px = max(16, min(56, px))
        bottom = self.y() + self.height()
        self.cfg["font_px"] = px
        h = self._heights()
        self.setGeometry(self.x(), bottom - h, self.width(), h)
        self._place_bar()
        self.update()
        self.settings_changed.emit({"font_px": px})

    def _toggle_german(self, on: bool):
        bottom = self.y() + self.height()
        self.cfg["show_german"] = on
        h = self._heights()
        self.setGeometry(self.x(), bottom - h, self.width(), h)
        self.update()
        self.settings_changed.emit({"show_german": on})

    def set_show_german(self, on: bool):
        self.buttons["german"].setChecked(on)

    def set_opacity(self, value: float):
        self.cfg["overlay_opacity"] = value
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_capture_affinity()

    def _apply_capture_affinity(self):
        """WDA_EXCLUDEFROMCAPTURE: the bar stays visible locally but is left out of screen sharing."""
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(self.winId())), 0x11 if self.cfg.get("hide_from_capture", True) else 0)
        except Exception:
            pass

    def set_hide_from_capture(self, on: bool):
        self.cfg["hide_from_capture"] = on
        self._apply_capture_affinity()
        self.settings_changed.emit({"hide_from_capture": on})

    def set_click_through(self, on: bool, save: bool = True):
        visible = self.isVisible()
        self.cfg["click_through"] = on
        self.setWindowFlag(Qt.WindowTransparentForInput, on)
        if visible:
            self.show()
        if save:
            self.settings_changed.emit({"click_through": on})

    # ---------------------------------------------------------------- content
    def set_message(self, text: str):
        self.message = text
        self.update()

    def set_phase(self, phase: str):
        self.phase = phase
        self.update()

    def set_paused(self, paused: bool):
        self.paused = paused
        self.buttons["pause"].setText("▶" if paused else "⏸")
        self.update()

    def set_speaking(self, speaking: bool):
        if speaking != self.speaking:
            self.speaking = speaking
            self.update()

    def show_caption(self, ev: dict):
        if ev["id"] != self.cur_id:
            if self.cur_zh and self.cur_state == "final":
                self.prev_zh = self.cur_zh
            self.cur_id = ev["id"]
            if ev.get("zh") is None:
                self.cur_zh = ""
        if ev.get("zh") is not None:
            self.cur_zh = ev["zh"]
        self.cur_de = ev["de"]
        self.cur_state = ev["state"]
        self.message = ""
        self.last_update = time.time()
        self.update()

    def drop_caption(self, cid: str):
        if cid == self.cur_id:
            self.cur_id, self.cur_zh, self.cur_de, self.cur_state = None, "", "", "idle"
            self.update()

    def clear(self):
        self.prev_zh, self.cur_id, self.cur_zh, self.cur_de, self.cur_state = "", None, "", "", "idle"
        self.update()

    # ---------------------------------------------------------------- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 16, 16)
        bg = QColor(13, 16, 22)
        bg.setAlphaF(max(0.2, min(1.0, self.cfg.get("overlay_opacity", 0.82))))
        p.fillPath(path, bg)
        p.setPen(QColor(255, 255, 255, 28))
        p.drawPath(path)

        zh, prev, de, small = self._fonts()
        stale = not self.speaking and time.time() - self.last_update > 12 and self.cur_state == "final"
        if self.paused:
            accent, label = IDLE, "已暂停"
        elif self.cur_state == "provisional":
            accent, label = PROVISIONAL, "识别中 · 停顿后定稿"
        elif self.cur_state == "final":
            accent, label = FINAL, "定稿" if not stale else "等待发言"
        else:
            accent, label = IDLE, {"loading": "加载中", "stopping": "正在停止", "error": "出错", "idle": "未启动"}.get(self.phase, "等待发言")
        if self.speaking and not self.paused and self.cur_state != "provisional":
            accent, label = PROVISIONAL, "正在听…"

        # status pill
        p.setFont(small)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QRectF(22, 19, 8, 8))
        p.setPen(QColor(200, 208, 220, 200))
        source = LANGUAGES.get(self.cfg.get("asr_language", "de"), "原文")
        p.drawText(QRectF(36, 12, 400, 22), Qt.AlignVCenter | Qt.AlignLeft, f"{source} → 中文 · {label}")

        left, right = 34, self.width() - 26
        width = right - left
        y_bottom = self.height() - 18
        fm_zh, fm_prev, fm_de = QFontMetrics(zh), QFontMetrics(prev), QFontMetrics(de)
        blocks = []  # (font, color, lines, spacing)
        if self.message and not self.cur_zh:
            blocks.append((zh, QColor(225, 230, 238, 190), tail_lines(self.message, zh, width, 2), fm_zh.lineSpacing()))
        else:
            dim = 0.55 if stale else 1.0
            if self.prev_zh:
                c = QColor(222, 228, 236)
                c.setAlphaF(0.50 * dim)
                blocks.append((prev, c, [fm_prev.elidedText(self.prev_zh, Qt.ElideLeft, int(width))], fm_prev.lineSpacing()))
            main_color = QColor(255, 255, 255) if self.cur_state == "final" else QColor(240, 244, 250)
            main_color.setAlphaF((1.0 if self.cur_state == "final" else 0.88) * dim)
            zh_text = self.cur_zh or ("…" if self.cur_de else "")
            blocks.append((zh, main_color, tail_lines(zh_text, zh, width, self.cfg.get("main_lines", 2)), fm_zh.lineSpacing()))
            if self.cfg.get("show_german", True) and self.cur_de:
                c = QColor(150, 168, 194)
                c.setAlphaF(0.95 * dim)
                blocks.append((de, c, tail_lines(self.cur_de, de, width, 2), fm_de.lineSpacing()))

        total = sum(len(lines) * sp for _, _, lines, sp in blocks) + 6 * (len(blocks) - 1)
        y = y_bottom - total
        main_top = main_bottom = None
        for font, color, lines, sp in blocks:
            p.setFont(font)
            if font is zh:
                main_top, main_bottom = y, y + len(lines) * sp
            for line in lines:
                rect = QRectF(left, y, width, sp)
                p.setPen(QColor(0, 0, 0, int(150 * color.alphaF() ** 2)))
                p.drawText(rect.translated(0, 1.5), Qt.AlignLeft | Qt.AlignVCenter, line)
                p.setPen(color)
                p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, line)
                y += sp
            y += 6
        if main_top is not None and (self.cur_zh or self.cur_de):
            p.setPen(Qt.NoPen)
            a = QColor(accent)
            a.setAlphaF(0.9 if not stale else 0.4)
            p.setBrush(a)
            p.drawRoundedRect(QRectF(18, main_top + 4, 4, max(8, main_bottom - main_top - 8)), 2, 2)
        p.end()

    # ---------------------------------------------------------------- mouse
    def _edge_at(self, pos: QPoint):
        if pos.x() <= self.EDGE:
            return "left"
        if pos.x() >= self.width() - self.EDGE:
            return "right"
        if pos.y() <= 6:
            return "top"
        if pos.y() >= self.height() - 6:
            return "bottom"
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        edge = self._edge_at(e.position().toPoint())
        if edge:
            self._resize = edge
            self._geom0 = (self.geometry(), e.globalPosition().toPoint())
        else:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        gp = e.globalPosition().toPoint()
        if self._drag is not None:
            self.move(gp - self._drag)
        elif self._resize:
            g, p0 = self._geom0
            dx = gp.x() - p0.x()
            if self._resize in ("top", "bottom"):
                dy = gp.y() - p0.y()
                n = self._lines_for_height(g.height() + (dy if self._resize == "bottom" else -dy))
                if n != self.cfg.get("main_lines", 2):
                    self.cfg["main_lines"] = n
                    h = self._heights()
                    y = g.y() if self._resize == "bottom" else g.y() + g.height() - h
                    self.setGeometry(g.x(), y, g.width(), h)
                    self.update()
            elif self._resize == "right":
                self.resize(max(420, g.width() + dx), self.height())
            else:
                w = max(420, g.width() - dx)
                self.setGeometry(g.right() - w + 1, g.y(), w, self.height())
        else:
            edge = self._edge_at(e.position().toPoint())
            self.setCursor(Qt.SizeHorCursor if edge in ("left", "right") else Qt.SizeVerCursor if edge else Qt.SizeAllCursor)

    def mouseReleaseEvent(self, e):
        if self._drag is not None or self._resize:
            self._save_geometry()
            if self._resize in ("top", "bottom"):
                self.settings_changed.emit({"main_lines": self.cfg["main_lines"]})
        self._drag = None
        self._resize = None

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_font(self.cfg["font_px"] + (2 if e.angleDelta().y() > 0 else -2))

    def _check_hover(self):
        inside = self.geometry().contains(QCursor.pos()) and not self.cfg.get("click_through")
        if inside != self.bar.isVisible():
            self.bar.setVisible(inside)
