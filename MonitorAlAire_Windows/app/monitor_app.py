"""
Monitor Al Aire - programa de escritorio.

Ventana con una tarjeta por estación (antena con ondas que se ve al aire / fuera
del aire), icono junto al reloj de Windows y configuración sencilla:
primero se crean las estaciones y luego se les agregan sus streams.
"""

import hashlib
import math
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request

# El Python portátil no agrega la carpeta del script a sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import (QPointF, QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QFontDatabase, QFontMetricsF,
                           QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QDoubleSpinBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
                               QGridLayout)

import motor

APP_NAME = "Monitor Al Aire"
INSTANCE_KEY = "MonitorAlAire-instancia"

# ---------------------------------------------------------------------------
# Estilo
# ---------------------------------------------------------------------------

C_BG_TOP = QColor("#0C1122")
C_BG_BOTTOM = QColor("#060913")
C_TEXT = QColor("#F2F5FF")
C_MUTED = QColor("#8C96B5")
C_DIM = QColor("#5C6684")
C_ACCENT = QColor("#7C83FF")

STATE_COLOR = {
    "aire": QColor("#2EE59D"),
    "parcial": QColor("#FFB547"),
    "verificando": QColor("#FFB547"),
    "fuera": QColor("#FF4D6A"),
    "conectando": QColor("#7A86A8"),
    "sin_streams": QColor("#7A86A8"),
}
STATE_LABEL = {
    "aire": "AL AIRE",
    "parcial": "AL AIRE",
    "verificando": "VERIFICANDO",
    "fuera": "FUERA DEL AIRE",
    "conectando": "CONECTANDO",
    "sin_streams": "SIN STREAMS",
}
STREAM_COLOR = {
    "audio": QColor("#2EE59D"),
    "silencio": QColor("#FFB547"),
    "sin_conexion": QColor("#FF4D6A"),
    "conectando": QColor("#7A86A8"),
}
STREAM_LABEL = {"silencio": "Silencio", "sin_conexion": "Sin conexión", "conectando": "Conectando…"}


def with_alpha(c: QColor, a: int) -> QColor:
    c = QColor(c)
    c.setAlpha(max(0, min(255, int(a))))
    return c


def font(px: float, weight=QFont.Weight.Normal, display=False, spacing=0.0) -> QFont:
    f = QFont("Inter Display" if display else "Inter")
    f.setPixelSize(max(1, int(round(px))))
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def fmt_dur(s: int) -> str:
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


QSS = """
* { font-family: "Inter"; color: #E7ECF8; }
QDialog, #page { background: #0B1020; }
QLabel#h1 { font-family: "Inter Display"; font-size: 22px; font-weight: 600; color: #F2F5FF; }
QLabel#h2 { font-size: 15px; font-weight: 600; color: #F2F5FF; }
QLabel#muted { color: #8C96B5; font-size: 13px; }
QLabel#ok { color: #2EE59D; font-size: 13px; }
QLabel#err { color: #FF7A90; font-size: 13px; }
QLineEdit, QDoubleSpinBox {
    background: #121933; border: 1px solid #222B4A; border-radius: 10px;
    padding: 9px 12px; font-size: 14px; selection-background-color: #7C83FF;
}
QLineEdit:focus, QDoubleSpinBox:focus { border: 1px solid #7C83FF; }
QLineEdit#title { background: transparent; border: 1px solid transparent;
    font-family: "Inter Display"; font-size: 22px; font-weight: 600; padding: 4px 8px; }
QLineEdit#title:hover { border: 1px solid #222B4A; }
QLineEdit#title:focus { border: 1px solid #7C83FF; background: #121933; }
QPushButton {
    background: #7C83FF; color: white; border: none; border-radius: 10px;
    padding: 9px 16px; font-size: 14px; font-weight: 600;
}
QPushButton:hover { background: #8D93FF; }
QPushButton:pressed { background: #6A71F0; }
QPushButton#ghost { background: #161E3A; border: 1px solid #263055; color: #E7ECF8; }
QPushButton#ghost:hover { background: #1C2547; }
QPushButton#danger { background: transparent; border: 1px solid #5A2433; color: #FF8EA0; }
QPushButton#danger:hover { background: #2A1320; }
QPushButton#icon { background: transparent; border: none; border-radius: 8px; padding: 6px; color: #8C96B5; font-size: 16px; }
QPushButton#icon:hover { background: #2A1320; color: #FF8EA0; }
QListWidget#nav { background: #080C18; border: none; border-right: 1px solid #1A2140; padding: 14px 10px; outline: 0; }
QListWidget#nav::item { padding: 11px 14px; border-radius: 10px; margin: 2px 0; color: #8C96B5; font-size: 14px; font-weight: 500; }
QListWidget#nav::item:selected { background: #161E3A; color: #FFFFFF; }
QListWidget#nav::item:hover:!selected { background: #10172E; }
QListWidget#stations { background: transparent; border: none; outline: 0; }
QListWidget#stations::item { padding: 12px 14px; border-radius: 12px; margin: 3px 0; font-size: 14px; color: #C9D0E6; }
QListWidget#stations::item:selected { background: #1A2246; color: #FFFFFF; }
QListWidget#stations::item:hover:!selected { background: #121933; }
QFrame#panel { background: #0F1530; border: 1px solid #1C2448; border-radius: 16px; }
QFrame#row { background: #121933; border: 1px solid #1E274A; border-radius: 12px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #263055; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QMenu { background: #121933; border: 1px solid #263055; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 8px 18px; border-radius: 6px; }
QMenu::item:selected { background: #1F2850; }
QMessageBox { background: #0B1020; }
QMessageBox QPushButton { min-width: 80px; }
"""


# ---------------------------------------------------------------------------
# Dibujo del emblema (antena con ondas)
# ---------------------------------------------------------------------------

def draw_emblem(p: QPainter, cx: float, cy: float, R: float, state: str, t: float,
                ripples=True, glow=True):
    col = STATE_COLOR.get(state, STATE_COLOR["conectando"])
    on = state in ("aire", "parcial")

    # Resplandor
    breathe = 0.5 + 0.5 * math.sin(t * 2.2) if state == "fuera" else 1.0
    glow_a = {"aire": 70, "parcial": 60, "verificando": 45, "fuera": 40 + 50 * breathe}.get(state, 18)
    g = QRadialGradient(QPointF(cx, cy), R * 2.6)
    g.setColorAt(0.0, with_alpha(col, glow_a))
    g.setColorAt(0.45, with_alpha(col, glow_a * 0.35))
    g.setColorAt(1.0, with_alpha(col, 0))
    if glow:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), R * 2.6, R * 2.6)

    # Ondas que salen de la antena
    if on and ripples:
        for i in range(3):
            ph = (t / 2.7 + i / 3.0) % 1.0
            r = R * (1.02 + ph * 1.35)
            a = (1 - ph) ** 1.8 * 150
            p.setPen(QPen(with_alpha(col, a), max(1.0, R * 0.035)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

    # Anillo exterior fino
    p.setPen(QPen(with_alpha(col, 70 if on else 45), max(1.0, R * 0.02)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), R * 1.12, R * 1.12)

    # Disco
    light = col.lighter(135)
    dark = col.darker(260)
    if state in ("conectando", "sin_streams"):
        light, dark = QColor("#4A5578"), QColor("#1A2038")
    dg = QRadialGradient(QPointF(cx - R * 0.35, cy - R * 0.45), R * 1.6)
    dg.setColorAt(0.0, light)
    dg.setColorAt(0.55, col if state not in ("conectando", "sin_streams") else QColor("#2C3554"))
    dg.setColorAt(1.0, dark)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(dg))
    p.drawEllipse(QPointF(cx, cy), R, R)
    # Brillo superior
    hl = QLinearGradient(QPointF(cx, cy - R), QPointF(cx, cy + R * 0.2))
    hl.setColorAt(0.0, QColor(255, 255, 255, 70))
    hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hl))
    p.drawEllipse(QPointF(cx, cy - R * 0.06), R * 0.9, R * 0.86)
    p.setPen(QPen(QColor(255, 255, 255, 50), max(1.0, R * 0.018)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), R - 0.5, R - 0.5)

    # Icono de transmisión
    white = QColor(255, 255, 255, 245)
    pen_w = max(1.6, R * 0.085)
    if state == "sin_streams":
        p.setPen(QPen(QColor(255, 255, 255, 200), pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - R * 0.3, cy), QPointF(cx + R * 0.3, cy))
        p.drawLine(QPointF(cx, cy - R * 0.3), QPointF(cx, cy + R * 0.3))
        return
    icon_alpha = 245 if state != "conectando" else 150
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(with_alpha(white, icon_alpha))
    p.drawEllipse(QPointF(cx, cy), R * 0.13, R * 0.13)
    for k, rr in enumerate((0.36, 0.58)):
        a = icon_alpha
        if state == "verificando":
            a = 90 + 155 * (0.5 + 0.5 * math.sin(t * 4 - k * 1.3))
        elif on:
            a = 170 + 75 * (0.5 + 0.5 * math.sin(t * 2.6 - k * 1.1))
        pen = QPen(with_alpha(white, a), pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        rect = QRectF(cx - R * rr, cy - R * rr, 2 * R * rr, 2 * R * rr)
        p.drawArc(rect, int(-42 * 16), int(84 * 16))
        p.drawArc(rect, int(138 * 16), int(84 * 16))
    if state == "fuera":
        p.setPen(QPen(QColor(255, 255, 255, 250), pen_w * 1.1, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - R * 0.55, cy - R * 0.55), QPointF(cx + R * 0.55, cy + R * 0.55))

    # Indicador giratorio (conectando / verificando)
    if state in ("conectando", "verificando"):
        p.setPen(QPen(with_alpha(col, 230), max(1.5, R * 0.05), Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        rr = R * 1.12
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(-t * 260 * 16) % (360 * 16), 70 * 16)


class LogoCache:
    """Carga los logos de las estaciones (archivo local o URL) y guarda versiones
    redondas ya escaladas para no recalcularlas en cada cuadro de la animación."""

    def __init__(self):
        self.images: dict[str, QImage | None] = {}
        self.pixmaps: dict[tuple, QPixmap] = {}
        self.lock = threading.Lock()

    @staticmethod
    def local_path(logo: str) -> str | None:
        if not logo:
            return None
        if logo.startswith(("http://", "https://")):
            ext = os.path.splitext(logo.split("?")[0])[1][:5] or ".img"
            return os.path.join(motor.DATA_DIR, "logos",
                                "url_" + hashlib.md5(logo.encode()).hexdigest()[:12] + ext)
        if os.path.isabs(logo):
            return logo
        for base in (motor.DATA_DIR, motor.APP_DIR):
            path = os.path.join(base, logo)
            if os.path.exists(path):
                return path
        return os.path.join(motor.APP_DIR, logo)

    def image(self, logo: str) -> QImage | None:
        if not logo:
            return None
        with self.lock:
            if logo in self.images:
                return self.images[logo]
            self.images[logo] = None
        path = self.local_path(logo)
        if os.path.exists(path):
            self._load(logo, path)
        elif logo.startswith(("http://", "https://")):
            threading.Thread(target=self._download, args=(logo, path), daemon=True).start()
        return self.images.get(logo)

    def _load(self, logo: str, path: str):
        img = QImage(path)
        if img.isNull():
            return
        if img.width() > 600:
            img = img.scaled(600, 600, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        with self.lock:
            self.images[logo] = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def _download(self, logo: str, path: str):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            req = urllib.request.Request(logo, headers={"User-Agent": "MonitorAlAire"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            with open(path, "wb") as f:
                f.write(data)
            self._load(logo, path)
        except Exception as e:
            motor.log.warning(f"No se pudo descargar el logo {logo}: {e}")
            with self.lock:
                self.images.pop(logo, None)  # se reintentará más tarde

    def forget(self, logo: str):
        with self.lock:
            self.images.pop(logo, None)
            self.pixmaps = {k: v for k, v in self.pixmaps.items() if k[0] != logo}

    def round_pixmap(self, logo: str, diameter: float, gray: bool, dpr: float) -> QPixmap | None:
        img = self.image(logo)
        if img is None or diameter < 4:
            return None
        px = max(4, int(round(diameter * dpr)))
        key = (logo, px, gray)
        pm = self.pixmaps.get(key)
        if pm is None:
            src = img
            if gray:
                src = img.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(
                    QImage.Format.Format_ARGB32_Premultiplied)
            # Acercar al centro del logo (los bordes suelen ser decoración)
            zoom = int(px * 1.28)
            src = src.scaled(zoom, zoom, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
            pm = QPixmap(px, px)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addEllipse(QRectF(0, 0, px, px))
            q.setClipPath(clip)
            q.fillRect(QRectF(0, 0, px, px), QColor("white"))
            q.drawImage(QPointF((px - src.width()) / 2, (px - src.height()) / 2), src)
            q.end()
            pm.setDevicePixelRatio(dpr)
            if len(self.pixmaps) > 200:
                self.pixmaps.clear()
            self.pixmaps[key] = pm
        return pm


LOGOS = LogoCache()


def draw_broadcast_icon(p: QPainter, cx: float, cy: float, R: float, alpha: int, slash: bool):
    white = QColor(255, 255, 255, alpha)
    pen_w = max(1.2, R * 0.12)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(white)
    p.drawEllipse(QPointF(cx, cy), R * 0.16, R * 0.16)
    p.setPen(QPen(white, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for rr in (0.42, 0.7):
        rect = QRectF(cx - R * rr, cy - R * rr, 2 * R * rr, 2 * R * rr)
        p.drawArc(rect, int(-42 * 16), int(84 * 16))
        p.drawArc(rect, int(138 * 16), int(84 * 16))
    if slash:
        p.drawLine(QPointF(cx - R * 0.62, cy - R * 0.62), QPointF(cx + R * 0.62, cy + R * 0.62))


def draw_logo_emblem(p: QPainter, cx: float, cy: float, R: float, state: str, t: float,
                     logo: str) -> bool:
    """Emblema con el logo de la estación. Devuelve False si el logo aún no está listo."""
    dpr = p.device().devicePixelRatioF() if p.device() else 1.0
    gray = state in ("fuera", "conectando", "sin_streams")
    inner = R * 0.86
    pm = LOGOS.round_pixmap(logo, inner * 2, gray, dpr)
    if pm is None:
        return False
    col = STATE_COLOR.get(state, STATE_COLOR["conectando"])
    on = state in ("aire", "parcial")

    # Resplandor y ondas (igual que el emblema dibujado)
    breathe = 0.5 + 0.5 * math.sin(t * 2.2) if state == "fuera" else 1.0
    glow_a = {"aire": 70, "parcial": 60, "verificando": 45, "fuera": 40 + 50 * breathe}.get(state, 18)
    g = QRadialGradient(QPointF(cx, cy), R * 2.5)
    g.setColorAt(0.0, with_alpha(col, glow_a))
    g.setColorAt(0.45, with_alpha(col, glow_a * 0.35))
    g.setColorAt(1.0, with_alpha(col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(cx, cy), R * 2.5, R * 2.5)
    if on:
        for i in range(3):
            ph = (t / 2.7 + i / 3.0) % 1.0
            r = R * (1.04 + ph * 1.3)
            p.setPen(QPen(with_alpha(col, (1 - ph) ** 1.8 * 150), max(1.0, R * 0.03)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

    # Aro de color del estado
    ring = QRadialGradient(QPointF(cx - R * 0.4, cy - R * 0.5), R * 2.2)
    ring.setColorAt(0, col.lighter(135))
    ring.setColorAt(1, col.darker(170))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(ring) if state not in ("conectando", "sin_streams") else QColor("#39425F"))
    p.drawEllipse(QPointF(cx, cy), R, R)

    # Logo
    p.save()
    if state == "conectando":
        p.setOpacity(0.55)
    p.drawPixmap(QRectF(cx - inner, cy - inner, inner * 2, inner * 2), pm, QRectF(pm.rect()))
    p.restore()
    if state == "fuera":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 0, 12, 70 + int(40 * breathe)))
        p.drawEllipse(QPointF(cx, cy), inner, inner)
    # Brillo sutil
    hl = QLinearGradient(QPointF(cx, cy - inner), QPointF(cx, cy))
    hl.setColorAt(0, QColor(255, 255, 255, 40))
    hl.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hl))
    p.drawEllipse(QPointF(cx, cy), inner, inner)

    # Insignia de estado (abajo a la derecha)
    if state in ("fuera", "parcial", "verificando", "aire"):
        bx, by = cx + R * 0.71, cy + R * 0.71
        br = R * 0.27
        p.setPen(QPen(QColor("#0E1428"), max(1.5, R * 0.05)))
        p.setBrush(col)
        p.drawEllipse(QPointF(bx, by), br, br)
        if state == "fuera":
            draw_broadcast_icon(p, bx, by, br * 0.9, 250, True)
        elif state == "aire":
            draw_broadcast_icon(p, bx, by, br * 0.9, 250, False)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 250), max(1.2, br * 0.2), Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(bx, by - br * 0.45), QPointF(bx, by + br * 0.08))
            p.drawPoint(QPointF(bx, by + br * 0.45))

    # Indicador giratorio (conectando / verificando)
    if state in ("conectando", "verificando"):
        p.setPen(QPen(with_alpha(col, 230), max(1.5, R * 0.05), Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = R * 1.12
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(-t * 260 * 16) % (360 * 16), 70 * 16)
    return True


def make_icon(state: str, size=256) -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_emblem(p, s / 2, s / 2, s * 0.44, state, 0.6, ripples=False, glow=False)
        p.end()
        icon.addPixmap(pm)
    return icon


# ---------------------------------------------------------------------------
# Tarjeta de estación
# ---------------------------------------------------------------------------

class StationCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: dict = {"nombre": "", "estado": "conectando", "streams": []}
        self.levels: dict[str, float] = {}
        self.seed = random.random() * 100
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def set_data(self, d: dict):
        self.data = d
        lines = []
        for st in d.get("streams", []):
            txt = {"audio": "con audio", "silencio": "en silencio", "conectando": "conectando…",
                   "sin_conexion": "sin conexión"}.get(st["estado"], st["estado"])
            line = f"{st['nombre']}: {txt}\n   {st['url']}"
            if st.get("error"):
                line += f"\n   Motivo: {st['error']}"
            lines.append(line)
        tip = "\n".join(lines)
        if tip != self.toolTip():
            self.setToolTip(tip)

    def paintEvent(self, _):
        t = time.monotonic() + self.seed
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        d = self.data
        state = d.get("estado", "conectando")
        col = STATE_COLOR.get(state, STATE_COLOR["conectando"])
        rad = clamp(min(w, h) * 0.075, 14, 24)
        card = QRectF(0.5, 0.5, w - 1, h - 1)

        # Fondo de la tarjeta
        bg = QLinearGradient(QPointF(0, 0), QPointF(0, h))
        bg.setColorAt(0, QColor(22, 29, 54, 235))
        bg.setColorAt(1, QColor(13, 18, 36, 235))
        path = QPainterPath()
        path.addRoundedRect(card, rad, rad)
        p.fillPath(path, QBrush(bg))
        p.save()
        p.setClipPath(path)

        streams = d.get("streams", [])
        sub = ""
        if state == "fuera":
            sub = f"Desde las {d.get('fuera_desde', '')[:5]} · {fmt_dur(d.get('fuera_seg', 0))}"
        elif state == "parcial":
            bad = sum(1 for s in streams if s["estado"] != "audio")
            sub = f"{bad} stream con falla" if bad == 1 else f"{bad} streams con falla"
        elif state == "sin_streams":
            sub = "Agrégale streams en Configuración"

        # Tarjeta ancha: logo a la izquierda y datos a la derecha
        if w > h * 1.3 and h >= 140:
            self._paint_horizontal(p, w, h, d, state, col, card, streams, sub, t)
        else:
            self._paint_vertical(p, w, h, d, state, col, card, streams, sub, t)

        p.restore()
        # Borde
        border = QLinearGradient(QPointF(0, 0), QPointF(0, h))
        if state == "fuera":
            border.setColorAt(0, with_alpha(col, 200))
            border.setColorAt(1, with_alpha(col, 70))
        else:
            border.setColorAt(0, QColor(255, 255, 255, 34))
            border.setColorAt(1, QColor(255, 255, 255, 8))
        p.setPen(QPen(QBrush(border), 1.2 if state == "fuera" else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card, rad, rad)
        p.end()

    def _paint_vertical(self, p, w, h, d, state, col, card, streams, sub, t):
        pad = clamp(min(w, h) * 0.065, 12, 26)
        n = len(streams)
        row_h = clamp(h * 0.085, 26, 38)
        gap = clamp(h * 0.018, 4, 8)
        base_px = clamp(min(w * 0.072, h * 0.08), 13, 32)
        name_px = self._fit_name_px(base_px, w - 2 * pad)
        pill_px = clamp(base_px * 0.44, 9.5, 13.5)
        sub_px = clamp(base_px * 0.5, 10.5, 14)
        # Si no cabe todo, se compacta por pasos (nunca se encima ni se corta):
        # 1) streams como filas  2) streams como puntos  3) sin subtítulo  4) sin streams
        min_emblem = 16 * 2.5
        for level in range(4):
            compact_streams = level >= 1
            show_sub = level < 2
            show_streams = level < 3 and n > 0
            if not show_streams:
                streams_h = 0
            elif compact_streams:
                streams_h = clamp(h * 0.09, 14, 22)
            else:
                streams_h = n * row_h + max(0, n - 1) * gap
            text_block = name_px * 1.25 + pill_px * 2.6 + (sub_px * 1.6 if show_sub else 0) + name_px * 0.9
            hero = QRectF(pad, pad, w - 2 * pad,
                          h - 2 * pad - streams_h - (gap * 2 if show_streams else 0))
            if hero.height() >= text_block + min_emblem and (level > 0 or streams_h <= h * 0.42):
                break
        if not show_sub:
            sub = ""
        R = clamp(min(hero.width() * 0.26, (hero.height() - text_block) / 2.5), 8, 150)
        total = R * 2.5 + text_block
        top = hero.top() + max(0, (hero.height() - total) / 2)
        cx, cy = w / 2, top + R * 1.25
        self._paint_glow(p, card, cx, cy, w, h, state, col)
        if not draw_logo_emblem(p, cx, cy, R, state, t, d.get("logo", "")):
            draw_emblem(p, cx, cy, R, state, t)

        y = cy + R * 1.25 + name_px * 0.55
        self._draw_name(p, QRectF(pad, y, w - 2 * pad, name_px * 1.3), name_px,
                        Qt.AlignmentFlag.AlignCenter)
        y += name_px * 1.3 + name_px * 0.35
        ph = self._draw_pill(p, cx, y, state, col, pill_px, t, centered=True)
        y += ph + sub_px * 0.6
        if sub:
            self._draw_sub(p, QRectF(pad, y, w - 2 * pad, sub_px * 1.5), sub, sub_px, state,
                           Qt.AlignmentFlag.AlignCenter)
        if show_streams:
            sy = h - pad - streams_h
            if compact_streams:
                self._draw_streams_compact(p, QRectF(pad, sy, w - 2 * pad, streams_h), streams)
            else:
                for i, s in enumerate(streams):
                    self._draw_stream_row(p, QRectF(pad, sy + i * (row_h + gap), w - 2 * pad, row_h), s, t)

    def _paint_horizontal(self, p, w, h, d, state, col, card, streams, sub, t):
        pad = clamp(h * 0.08, 14, 30)
        n = len(streams)
        left_w = min(h - 2 * pad, w * 0.38)
        R = clamp(min(left_w * 0.35, (h - 2 * pad) * 0.35), 10, 150)
        cx, cy = pad + left_w / 2, h / 2
        self._paint_glow(p, card, cx, cy, w, h, state, col)
        if not draw_logo_emblem(p, cx, cy, R, state, t, d.get("logo", "")):
            draw_emblem(p, cx, cy, R, state, t)

        x0 = pad + left_w + pad * 0.4
        rw = w - x0 - pad
        base_px = clamp(min(rw * 0.1, h * 0.09), 13, 34)
        name_px = self._fit_name_px(base_px, rw)
        pill_px = clamp(base_px * 0.46, 9.5, 14)
        sub_px = clamp(base_px * 0.52, 11, 15)
        row_h = clamp(h * 0.105, 24, 40)
        gap = clamp(h * 0.022, 4, 8)
        head_h = name_px * 1.3 + name_px * 0.3 + pill_px * 2.3
        for level in range(4):
            show_sub = bool(sub) and level < 2
            compact = level >= 1
            show_streams = n > 0 and level < 3
            if not show_streams:
                st_h = 0
            elif compact:
                st_h = clamp(h * 0.09, 14, 22)
            else:
                st_h = n * row_h + (n - 1) * gap
            total = head_h + (sub_px * 1.9 if show_sub else 0) + ((pad * 0.7 + st_h) if show_streams else 0)
            if total <= h - 2 * pad:
                break
        y = max(pad, (h - total) / 2)
        self._draw_name(p, QRectF(x0, y, rw, name_px * 1.3), name_px,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        y += name_px * 1.3 + name_px * 0.3
        ph = self._draw_pill(p, x0, y, state, col, pill_px, t, centered=False)
        y += ph
        if show_sub:
            y += sub_px * 0.4
            self._draw_sub(p, QRectF(x0, y, rw, sub_px * 1.5), sub, sub_px, state,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            y += sub_px * 1.5
        if show_streams:
            y += pad * 0.7
            if compact:
                self._draw_streams_compact(p, QRectF(x0, y, min(rw, 160), st_h), streams)
            else:
                for i, s in enumerate(streams):
                    self._draw_stream_row(p, QRectF(x0, y + i * (row_h + gap), rw, row_h), s, t)

    def _paint_glow(self, p, card, cx, cy, w, h, state, col):
        bgl = QRadialGradient(QPointF(cx, cy), max(w, h) * 0.75)
        bgl.setColorAt(0, with_alpha(col, 34 if state != "fuera" else 46))
        bgl.setColorAt(1, with_alpha(col, 0))
        p.fillRect(card, QBrush(bgl))

    def _fit_name_px(self, px: float, width: float) -> float:
        """Reduce la letra del nombre para que quepa completo (hasta 60%)."""
        tw = QFontMetricsF(font(px, QFont.Weight.DemiBold, display=True)).horizontalAdvance(
            self.data.get("nombre", ""))
        if tw > width > 0:
            px = max(px * 0.6, px * width / tw * 0.98)
        return px

    def _draw_name(self, p, rect: QRectF, px: float, align):
        p.setFont(font(px, QFont.Weight.DemiBold, display=True))
        name = QFontMetricsF(p.font()).elidedText(self.data.get("nombre", ""),
                                                   Qt.TextElideMode.ElideRight, rect.width())
        p.setPen(C_TEXT)
        p.drawText(rect, align, name)

    def _draw_sub(self, p, rect: QRectF, text: str, px: float, state: str, align):
        p.setFont(font(px, QFont.Weight.Medium))
        p.setPen(C_MUTED if state != "fuera" else QColor("#FFA3B1"))
        p.drawText(rect, align, text)

    def _draw_pill(self, p, x: float, y: float, state: str, col: QColor, pill_px: float,
                   t: float, centered: bool) -> float:
        pf = font(pill_px, QFont.Weight.Bold, spacing=pill_px * 0.12)
        p.setFont(pf)
        label = STATE_LABEL.get(state, state.upper())
        tw = QFontMetricsF(pf).horizontalAdvance(label)
        ph = pill_px * 2.3
        pw = tw + pill_px * 3.4
        pill = QRectF(x - pw / 2 if centered else x, y, pw, ph)
        p.setPen(QPen(with_alpha(col, 90), 1))
        p.setBrush(with_alpha(col, 30))
        p.drawRoundedRect(pill, ph / 2, ph / 2)
        dot_r = pill_px * 0.3
        dot_c = QPointF(pill.left() + pill_px * 1.25, pill.center().y())
        if state in ("aire", "parcial"):
            pulse = (t * 0.8) % 1.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(col, (1 - pulse) * 120))
            p.drawEllipse(dot_c, dot_r * (1 + pulse * 1.6), dot_r * (1 + pulse * 1.6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(dot_c, dot_r, dot_r)
        p.setPen(col)
        p.drawText(QRectF(pill.left() + pill_px * 2.0, pill.top(), tw + pill_px, ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        return ph

    def _level(self, s: dict) -> float:
        target = clamp((s["nivel"] + 55) / 35, 0.2, 1) if s["estado"] == "audio" else 0.0
        cur = self.levels.get(s["id"], 0.0)
        cur += (target - cur) * 0.18
        self.levels[s["id"]] = cur
        return cur

    def _draw_stream_row(self, p: QPainter, r: QRectF, s: dict, t: float):
        st = s["estado"]
        col = STREAM_COLOR.get(st, STREAM_COLOR["conectando"])
        rr = r.height() * 0.32
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))
        p.setBrush(QColor(255, 255, 255, 9))
        p.drawRoundedRect(r, rr, rr)
        fpx = clamp(r.height() * 0.4, 10.5, 13.5)
        dot_c = QPointF(r.left() + r.height() * 0.5, r.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(dot_c, fpx * 0.28, fpx * 0.28)

        # Visualizador del nivel (barras animadas) o texto del problema
        vis_w = clamp(r.width() * 0.34, 50, 130)
        vis = QRectF(r.right() - vis_w - r.height() * 0.4, r.top() + r.height() * 0.24,
                     vis_w, r.height() * 0.52)
        lvl = self._level(s)
        if (st == "audio" or lvl > 0.02) and r.width() < 170:
            text_right = r.right() - r.height() * 0.4  # fila angosta: sin barras
        elif st == "audio" or lvl > 0.02:
            bars = int(clamp(vis_w / 6, 8, 22))
            bw = vis.width() / bars
            for i in range(bars):
                k = i / max(1, bars - 1)
                wob = abs(math.sin(t * 6.1 + i * 1.7 + self.seed) * 0.6 + math.sin(t * 2.3 + i * 0.9) * 0.4)
                env = 0.55 + 0.45 * math.sin(math.pi * (0.1 + 0.8 * k))
                bh = max(2.0, vis.height() * clamp(lvl * env * (0.35 + 0.65 * wob), 0.1, 1.0))
                br = QRectF(vis.left() + i * bw + bw * 0.22, vis.center().y() - bh / 2, bw * 0.56, bh)
                p.setBrush(with_alpha(col, 120 + 135 * k * lvl))
                p.drawRoundedRect(br, bw * 0.28, bw * 0.28)
            text_right = vis.left() - 8
        else:
            p.setFont(font(fpx * 0.92, QFont.Weight.Medium))
            p.setPen(col)
            lbl = STREAM_LABEL.get(st, "")
            tw = QFontMetricsF(p.font()).horizontalAdvance(lbl)
            p.drawText(QRectF(r.right() - tw - r.height() * 0.45, r.top(), tw + 2, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, lbl)
            text_right = r.right() - tw - r.height() * 0.45 - 8
        p.setFont(font(fpx, QFont.Weight.Medium))
        p.setPen(QColor("#C3CAE0"))
        x0 = dot_c.x() + fpx * 0.9
        name = QFontMetricsF(p.font()).elidedText(s["nombre"], Qt.TextElideMode.ElideRight,
                                                   max(10, text_right - x0))
        p.drawText(QRectF(x0, r.top(), max(10, text_right - x0), r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)

    def _draw_streams_compact(self, p: QPainter, r: QRectF, streams: list):
        """Tarjeta muy chica: solo puntos de color por stream."""
        n = len(streams)
        d = clamp(r.height() * 0.42, 6, 10)
        gap = d * 1.4
        total = n * d + (n - 1) * gap
        x = r.center().x() - total / 2
        p.setPen(Qt.PenStyle.NoPen)
        for s in streams:
            self._level(s)
            p.setBrush(STREAM_COLOR.get(s["estado"], STREAM_COLOR["conectando"]))
            p.drawEllipse(QRectF(x, r.center().y() - d / 2, d, d))
            x += d + gap


class StationGrid(QWidget):
    """Acomoda las tarjetas para que SIEMPRE quepan en la ventana."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards: dict[str, StationCard] = {}
        self.order: list[str] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(200, 160)

    def set_stations(self, stations: list):
        ids = [s["id"] for s in stations]
        for sid in list(self.cards):
            if sid not in ids:
                self.cards.pop(sid).deleteLater()
        for s in stations:
            c = self.cards.get(s["id"])
            if not c:
                c = StationCard(self)
                c.show()
                self.cards[s["id"]] = c
            c.set_data(s)
        if ids != self.order:
            self.order = ids
            self.relayout()

    def resizeEvent(self, e):
        self.relayout()

    def relayout(self):
        n = len(self.order)
        if not n:
            return
        W, H = self.width(), self.height()
        gap = clamp(min(W, H) * 0.025, 10, 22)
        best = None
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            cw = (W - gap * (cols - 1)) / cols
            ch = (H - gap * (rows - 1)) / rows
            if cw <= 0 or ch <= 0:
                continue
            # tamaño útil de la tarjeta (proporción cómoda) y castigo por huecos
            score = min(cw, ch * 1.15) - (rows * cols - n) * 3
            if not best or score > best[0]:
                best = (score, cols, rows, cw, ch)
        _, cols, rows, cw, ch = best
        # Que no queden tarjetas exageradamente anchas o altas
        cw2 = min(cw, ch * 1.45)
        ch2 = min(ch, cw2 * 1.6)
        grid_w = cols * cw2 + (cols - 1) * gap
        grid_h = rows * ch2 + (rows - 1) * gap
        y0 = (H - grid_h) / 2
        for i, sid in enumerate(self.order):
            r, c = divmod(i, cols)
            in_row = min(cols, n - r * cols)
            row_w = in_row * cw2 + (in_row - 1) * gap
            x0 = (W - row_w) / 2 if r == rows - 1 else (W - grid_w) / 2
            self.cards[sid].setGeometry(int(x0 + c * (cw2 + gap)), int(y0 + r * (ch2 + gap)),
                                        int(cw2), int(ch2))

    def tick(self):
        for c in self.cards.values():
            c.update()


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------

class Chip(QWidget):
    """Indicador redondeado con punto de color (resumen en la barra superior)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.color = C_MUTED
        self.setFixedHeight(32)

    def set(self, text: str, color: QColor):
        if text != self.text or color != self.color:
            self.text, self.color = text, color
            f = font(13, QFont.Weight.Medium)
            self.setFixedWidth(int(QFontMetricsF(f).horizontalAdvance(text) + 38))
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 10))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QPointF(15, r.center().y()), 4, 4)
        p.setFont(font(13, QFont.Weight.Medium))
        p.setPen(QColor("#D5DBEE"))
        p.drawText(QRectF(26, 0, self.width() - 30, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)
        p.end()


class IconButton(QWidget):
    clicked = Signal()

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.hover = False
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Configuración")

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, 39, 39)
        p.setPen(QPen(QColor(255, 255, 255, 30 if self.hover else 20), 1))
        p.setBrush(QColor(255, 255, 255, 22 if self.hover else 10))
        p.drawRoundedRect(r, 12, 12)
        # engrane
        c = QPointF(20, 20)
        pen = QPen(QColor("#D5DBEE"), 1.7)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        teeth = 8
        for i in range(teeth * 2 + 1):
            a = math.pi * 2 * i / (teeth * 2)
            rad = 8.2 if i % 2 == 0 else 6.4
            for da in (-0.17, 0.17):
                pt = QPointF(c.x() + rad * math.cos(a + da), c.y() + rad * math.sin(a + da))
                if i == 0 and da < 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(c, 2.8, 2.8)
        p.end()


class Header(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(76)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 8)
        lay.setSpacing(10)
        self.logo = QLabel()
        self.logo.setFixedSize(34, 34)
        lay.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(APP_NAME)
        self.title.setStyleSheet('font-family:"Inter Display";font-size:19px;font-weight:600;color:#F2F5FF;')
        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet("font-size:12px;color:#7D88A8;")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        lay.addLayout(titles)
        lay.addStretch(1)
        self.chip_on = Chip()
        self.chip_off = Chip()
        self.chip_tg = Chip()
        self.clock = QLabel("--:--")
        self.clock.setStyleSheet('font-family:"Inter Display";font-size:15px;font-weight:500;color:#AEB7D2;padding:0 6px;')
        self.btn = IconButton("gear")
        for wdg in (self.chip_on, self.chip_off, self.chip_tg, self.clock, self.btn):
            lay.addWidget(wdg)

    def resizeEvent(self, e):
        w = self.width()
        self.subtitle.setVisible(w >= 560)
        self.title.setVisible(w >= 470)
        self.chip_tg.setVisible(w >= 820)
        self.clock.setVisible(w >= 640)
        super().resizeEvent(e)

    def update_status(self, st: dict):
        sts = st["estaciones"]
        on = sum(1 for s in sts if s["estado"] in ("aire", "parcial"))
        off = sum(1 for s in sts if s["estado"] == "fuera")
        self.chip_on.set(f"{on} al aire", STATE_COLOR["aire"])
        self.chip_off.setVisible(off > 0)
        self.chip_off.set(f"{off} fuera", STATE_COLOR["fuera"])
        self.chip_tg.set("Telegram activo" if st["telegram"] else "Telegram sin configurar",
                         STATE_COLOR["aire"] if st["telegram"] else C_DIM)
        self.clock.setText(st["hora"][:5])
        n = len(sts)
        self.subtitle.setText(f"{n} estación vigilada" if n == 1 else f"{n} estaciones vigiladas")
        state = "fuera" if off else "aire"
        if getattr(self, "_logo_state", None) != state:
            self._logo_state = state
            self.logo.setPixmap(make_icon(state).pixmap(QSize(34, 34)))


class Banner(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setStyleSheet("background:#2A1320;border:1px solid #5A2433;border-radius:12px;"
                           "color:#FFC2CC;font-size:13px;padding:10px 14px;")
        self.hide()


class EmptyState(QWidget):
    create = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        self.icon = QLabel()
        self.icon.setPixmap(make_icon("conectando").pixmap(QSize(96, 96)))
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = QLabel("Aún no hay estaciones")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet('font-family:"Inter Display";font-size:24px;font-weight:600;color:#F2F5FF;')
        s = QLabel("Crea tus estaciones y luego agrégale a cada una sus streams.")
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.setStyleSheet("font-size:14px;color:#8C96B5;")
        b = QPushButton("Crear estación")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(self.create.emit)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(b)
        row.addStretch(1)
        for wdg in (self.icon, t, s):
            lay.addWidget(wdg)
        lay.addSpacing(10)
        lay.addLayout(row)
        lay.addStretch(2)


class MainWindow(QMainWindow):
    def __init__(self, mon: motor.Monitor):
        super().__init__()
        self.mon = mon
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon("aire"))
        self.resize(1180, 760)
        self.setMinimumSize(420, 340)
        self.quitting = False
        self.tray_hint_shown = False

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.header = Header()
        self.header.btn.clicked.connect(lambda: self.open_settings())
        v.addWidget(self.header)
        self.banner = Banner()
        bw = QHBoxLayout()
        bw.setContentsMargins(28, 4, 28, 0)
        bw.addWidget(self.banner)
        v.addLayout(bw)
        self.stack = QStackedWidget()
        self.grid = StationGrid()
        self.empty = EmptyState()
        self.empty.create.connect(lambda: self.open_settings(new_station=True))
        gw = QWidget()
        gl = QVBoxLayout(gw)
        gl.setContentsMargins(28, 14, 28, 28)
        gl.addWidget(self.grid)
        self.stack.addWidget(gw)
        self.stack.addWidget(self.empty)
        v.addWidget(self.stack, 1)

        self.anim = QTimer(self)
        self.anim.timeout.connect(self.grid.tick)
        self.anim.start(33)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh)
        self.poll.start(500)

        # Icono junto al reloj
        self.tray = QSystemTrayIcon(make_icon("aire"), self)
        menu = QMenu()
        a_open = QAction("Abrir Monitor", self)
        a_open.triggered.connect(self.show_window)
        a_quit = QAction("Salir", self)
        a_quit.triggered.connect(self.quit_app)
        menu.addAction(a_open)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window()
                                    if r != QSystemTrayIcon.ActivationReason.Context else None)
        self.tray.setToolTip(APP_NAME)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._tray_state = "aire"
        self.refresh()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        g = QLinearGradient(QPointF(0, 0), QPointF(0, h))
        g.setColorAt(0, C_BG_TOP)
        g.setColorAt(1, C_BG_BOTTOM)
        p.fillRect(self.rect(), QBrush(g))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for (x, y, r, c) in ((0.08, -0.1, 0.55, QColor(92, 99, 255, 34)),
                             (1.05, 1.1, 0.6, QColor(46, 229, 157, 18))):
            rg = QRadialGradient(QPointF(w * x, h * y), max(w, h) * r)
            rg.setColorAt(0, c)
            rg.setColorAt(1, with_alpha(c, 0))
            p.fillRect(self.rect(), QBrush(rg))
        p.end()

    def refresh(self):
        st = self.mon.status()
        self.header.update_status(st)
        self.grid.set_stations(st["estaciones"])
        self.stack.setCurrentIndex(0 if st["estaciones"] else 1)
        msgs = []
        if not st["ffmpeg"]:
            msgs.append("No se encontró ffmpeg, así que no se puede medir el audio. "
                        "Abre el programa con «Iniciar Monitor» para que lo descargue.")
        if not st["internet"]:
            msgs.append("Esta PC no tiene internet. Las alertas están en pausa.")
        self.banner.setText("  ".join(msgs))
        self.banner.setVisible(bool(msgs))
        off = [s["nombre"] for s in st["estaciones"] if s["estado"] == "fuera"]
        tstate = "fuera" if off else "aire"
        if tstate != self._tray_state:
            self._tray_state = tstate
            self.tray.setIcon(make_icon(tstate))
            self.setWindowIcon(make_icon(tstate))
        self.tray.setToolTip(APP_NAME + ("\nFuera del aire: " + ", ".join(off) if off else "\nTodo al aire"))

    def open_settings(self, new_station=False):
        dlg = SettingsDialog(self.mon, self)
        if new_station:
            dlg.focus_new_station()
        dlg.exec()
        self.refresh()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.anim.start(33)

    def changeEvent(self, e):
        super().changeEvent(e)
        if not hasattr(self, "anim"):
            return
        if self.isMinimized():
            self.anim.stop()
        elif not self.anim.isActive():
            self.anim.start(33)

    def closeEvent(self, e):
        if self.quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            self.mon.shutdown()
            e.accept()
            return
        # Cerrar la ventana no detiene el monitor: queda junto al reloj
        e.ignore()
        self.hide()
        self.anim.stop()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "El monitor sigue vigilando. Lo encuentras junto al reloj.",
                                  make_icon("aire"), 4000)

    def quit_app(self):
        r = QMessageBox.question(self, APP_NAME, "¿Cerrar el monitor?\nDejará de vigilar las estaciones.")
        if r == QMessageBox.StandardButton.Yes:
            self.quitting = True
            self.tray.hide()
            self.mon.shutdown()
            QApplication.quit()


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

class StreamRow(QFrame):
    removed = Signal(str)
    renamed = Signal(str, str)

    def __init__(self, s: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("row")
        self.sid = s["id"]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.name = QLineEdit(s["nombre"])
        self.name.setStyleSheet("background:transparent;border:1px solid transparent;padding:2px 4px;"
                                "font-weight:600;font-size:14px;")
        self.name.setToolTip("Clic para cambiar el nombre")
        self.name.editingFinished.connect(lambda: self.renamed.emit(self.sid, self.name.text()))
        url = QLabel(s["url"])
        url.setStyleSheet("color:#7D88A8;font-size:12px;padding-left:5px;")
        url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.name)
        col.addWidget(url)
        lay.addLayout(col, 1)
        rm = QPushButton("✕")
        rm.setObjectName("icon")
        rm.setToolTip("Quitar stream")
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda: self.removed.emit(self.sid))
        lay.addWidget(rm)


class SettingsDialog(QDialog):
    def __init__(self, mon: motor.Monitor, parent=None):
        super().__init__(parent)
        self.mon = mon
        self.setWindowTitle("Configuración")
        self.resize(900, 600)
        self.setMinimumSize(640, 460)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(190)
        for name in ("Estaciones", "Telegram", "Correo", "Avanzado"):
            self.nav.addItem(QListWidgetItem(name))
        lay.addWidget(self.nav)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        self.pages.addWidget(self._page_stations())
        self.pages.addWidget(self._page_telegram())
        self.pages.addWidget(self._page_mail())
        self.pages.addWidget(self._page_advanced())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.reload_stations()

    # ---- Estaciones ----
    def _page_stations(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)
        h1 = QLabel("Estaciones")
        h1.setObjectName("h1")
        hint = QLabel("1. Crea la estación   ·   2. Selecciónala y agrégale sus streams")
        hint.setObjectName("muted")
        v.addWidget(h1)
        v.addWidget(hint)
        body = QHBoxLayout()
        body.setSpacing(16)
        # Lista de estaciones
        left = QFrame()
        left.setObjectName("panel")
        left.setFixedWidth(250)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 12, 12)
        lv.setSpacing(8)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("Nombre de la nueva estación")
        self.new_name.returnPressed.connect(self.add_station)
        add = QPushButton("+  Crear estación")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self.add_station)
        self.st_list = QListWidget()
        self.st_list.setObjectName("stations")
        self.st_list.currentRowChanged.connect(self.show_station)
        lv.addWidget(self.new_name)
        lv.addWidget(add)
        lv.addSpacing(4)
        lv.addWidget(self.st_list, 1)
        body.addWidget(left)
        # Detalle
        self.detail = QFrame()
        self.detail.setObjectName("panel")
        dv = QVBoxLayout(self.detail)
        dv.setContentsMargins(18, 16, 18, 18)
        dv.setSpacing(12)
        top = QHBoxLayout()
        self.st_title = QLineEdit()
        self.st_title.setObjectName("title")
        self.st_title.setToolTip("Clic para cambiar el nombre")
        self.st_title.editingFinished.connect(self.rename_station)
        self.del_st = QPushButton("Eliminar")
        self.del_st.setObjectName("danger")
        self.del_st.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_st.clicked.connect(self.delete_station)
        top.addWidget(self.st_title, 1)
        top.addWidget(self.del_st)
        dv.addLayout(top)
        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        self.logo_prev = QLabel()
        self.logo_prev.setFixedSize(48, 48)
        self.logo_url = QLineEdit()
        self.logo_url.setPlaceholderText("Logo: pega la URL o elige una imagen")
        self.logo_url.editingFinished.connect(self.set_logo_url)
        pick = QPushButton("Elegir imagen…")
        pick.setObjectName("ghost")
        pick.setCursor(Qt.CursorShape.PointingHandCursor)
        pick.clicked.connect(self.pick_logo)
        logo_row.addWidget(self.logo_prev)
        logo_row.addWidget(self.logo_url, 1)
        logo_row.addWidget(pick)
        dv.addLayout(logo_row)
        self.st_emails = QLineEdit()
        self.st_emails.setPlaceholderText("Correos que reciben las alertas de esta estación (separados por comas)")
        self.st_emails.editingFinished.connect(self.save_station_emails)
        dv.addWidget(self.st_emails)
        lbl = QLabel("Streams")
        lbl.setObjectName("h2")
        dv.addWidget(lbl)
        self.streams_box = QVBoxLayout()
        self.streams_box.setSpacing(8)
        sw = QWidget()
        sw.setObjectName("clear")
        sw.setLayout(self.streams_box)
        self.streams_box.setContentsMargins(0, 0, 4, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setWidget(sw)
        sc.viewport().setAutoFillBackground(False)
        sc.setStyleSheet("QScrollArea, QWidget#clear { background: transparent; }")
        dv.addWidget(sc, 1)
        addrow = QHBoxLayout()
        self.new_url = QLineEdit()
        self.new_url.setPlaceholderText("URL del stream  (http://...)")
        self.new_url.returnPressed.connect(self.add_stream)
        add_s = QPushButton("Agregar stream")
        add_s.setCursor(Qt.CursorShape.PointingHandCursor)
        add_s.clicked.connect(self.add_stream)
        addrow.addWidget(self.new_url, 1)
        addrow.addWidget(add_s)
        dv.addLayout(addrow)
        self.no_station = QLabel("Crea una estación a la izquierda para agregarle streams.")
        self.no_station.setObjectName("muted")
        self.no_station.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right = QStackedWidget()
        right.addWidget(self.detail)
        right.addWidget(self.no_station)
        self.right = right
        body.addWidget(right, 1)
        v.addLayout(body, 1)
        return page

    def focus_new_station(self):
        self.nav.setCurrentRow(0)
        QTimer.singleShot(50, self.new_name.setFocus)

    def current_station(self) -> dict | None:
        i = self.st_list.currentRow()
        sts = self.mon.cfg["estaciones"]
        return sts[i] if 0 <= i < len(sts) else None

    def reload_stations(self, select_id: str | None = None):
        cur = self.current_station()
        select_id = select_id or (cur["id"] if cur else None)
        self.st_list.blockSignals(True)
        self.st_list.clear()
        row = 0
        for i, st in enumerate(self.mon.cfg["estaciones"]):
            n = len(st["streams"])
            it = QListWidgetItem(f"{st['nombre']}\n{n} stream" + ("" if n == 1 else "s"))
            self.st_list.addItem(it)
            if st["id"] == select_id:
                row = i
        self.st_list.blockSignals(False)
        if self.st_list.count():
            self.st_list.blockSignals(True)
            self.st_list.setCurrentRow(row)
            self.st_list.blockSignals(False)
            self.show_station(row)
        else:
            self.right.setCurrentIndex(1)

    def show_station(self, _row=None):
        st = self.current_station()
        if not st:
            self.right.setCurrentIndex(1)
            return
        self.right.setCurrentIndex(0)
        self.st_title.setText(st["nombre"])
        self.logo_url.setText(st.get("logo", ""))
        self.st_emails.setText(", ".join(st.get("correos", [])))
        self._refresh_logo_preview(st.get("logo", ""))
        while self.streams_box.count():
            it = self.streams_box.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().setParent(None)
                it.widget().deleteLater()
        if not st["streams"]:
            e = QLabel("Esta estación aún no tiene streams. Pega abajo la URL del primero.")
            e.setObjectName("muted")
            e.setWordWrap(True)
            self.streams_box.addWidget(e)
        for s in st["streams"]:
            r = StreamRow(s)
            r.removed.connect(lambda sid, stid=st["id"]: self._del_stream(stid, sid))
            r.renamed.connect(lambda sid, name, stid=st["id"]: self.mon.update_stream(stid, sid, nombre=name))
            self.streams_box.addWidget(r)
        self.streams_box.addStretch(1)

    def _refresh_logo_preview(self, logo: str, tries: int = 0):
        pm = LOGOS.round_pixmap(logo, 48, False, self.devicePixelRatioF()) if logo else None
        if pm is None:
            pm = make_icon("conectando").pixmap(QSize(48, 48))
            if logo and tries < 20:  # puede estar descargándose
                QTimer.singleShot(500, lambda: self._refresh_logo_preview(logo, tries + 1))
        self.logo_prev.setPixmap(pm)

    def save_station_emails(self):
        st = self.current_station()
        if st:
            emails = [x for x in self.st_emails.text().replace(";", ",").replace(" ", ",").split(",") if x]
            if emails != st.get("correos", []):
                self.mon.set_station_emails(st["id"], emails)

    def set_logo_url(self):
        st = self.current_station()
        if not st:
            return
        logo = self.logo_url.text().strip()
        if logo != st.get("logo", ""):
            LOGOS.forget(logo)
            self.mon.set_station_logo(st["id"], logo)
            self._refresh_logo_preview(logo)

    def pick_logo(self):
        st = self.current_station()
        if not st:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Elegir logo", "",
                                              "Imágenes (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "Logo", "No se pudo abrir esa imagen.")
            return
        img = img.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        rel = f"logos/{st['id']}_{int(time.time())}.png"
        os.makedirs(os.path.join(motor.DATA_DIR, "logos"), exist_ok=True)
        img.save(os.path.join(motor.DATA_DIR, rel))
        self.mon.set_station_logo(st["id"], rel)
        self.logo_url.setText(rel)
        self._refresh_logo_preview(rel)

    def add_station(self):
        name = self.new_name.text().strip()
        if not name:
            self.new_name.setFocus()
            return
        st = self.mon.add_station(name)
        self.new_name.clear()
        self.reload_stations(st["id"])
        self.new_url.setFocus()

    def rename_station(self):
        st = self.current_station()
        if st and self.st_title.text().strip() and self.st_title.text().strip() != st["nombre"]:
            self.mon.rename_station(st["id"], self.st_title.text())
            self.reload_stations(st["id"])

    def delete_station(self):
        st = self.current_station()
        if not st:
            return
        r = QMessageBox.question(self, "Eliminar estación",
                                 f"¿Eliminar «{st['nombre']}» y todos sus streams?")
        if r == QMessageBox.StandardButton.Yes:
            self.mon.delete_station(st["id"])
            self.reload_stations()

    def add_stream(self):
        st = self.current_station()
        url = self.new_url.text().strip()
        if not st or not url:
            self.new_url.setFocus()
            return
        if "://" not in url:
            url = "http://" + url
        self.mon.add_stream(st["id"], url)
        self.new_url.clear()
        self.reload_stations(st["id"])
        self.new_url.setFocus()

    def _del_stream(self, stid, sid):
        self.mon.delete_stream(stid, sid)
        self.reload_stations(stid)

    # ---- Telegram ----
    def _page_telegram(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(12)
        h1 = QLabel("Alertas por Telegram")
        h1.setObjectName("h1")
        v.addWidget(h1)
        steps = QLabel("1. En Telegram abre <b>@BotFather</b>, envía <b>/newbot</b> y copia el token.<br>"
                       "2. Pega el token aquí y guarda.<br>"
                       "3. Envíale un mensaje a tu bot (o agrégalo a un grupo y escribe algo).<br>"
                       "4. Presiona <b>Detectar chats</b> y elige a dónde llegarán las alertas.")
        steps.setObjectName("muted")
        steps.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(steps)
        v.addSpacing(6)
        tg = self.mon.cfg["telegram"]
        v.addWidget(self._lbl("Token del bot"))
        r1 = QHBoxLayout()
        self.tg_token = QLineEdit(tg.get("token", ""))
        self.tg_token.setPlaceholderText("123456789:ABC...")
        b1 = QPushButton("Guardar")
        b1.clicked.connect(self.save_token)
        r1.addWidget(self.tg_token, 1)
        r1.addWidget(b1)
        v.addLayout(r1)
        v.addWidget(self._lbl("Chats que reciben las alertas"))
        r2 = QHBoxLayout()
        self.tg_chats = QLineEdit(", ".join(tg.get("chat_ids", [])))
        self.tg_chats.setPlaceholderText("ID del chat (varios separados por comas)")
        b2 = QPushButton("Guardar")
        b2.clicked.connect(self.save_chats)
        r2.addWidget(self.tg_chats, 1)
        r2.addWidget(b2)
        v.addLayout(r2)
        r3 = QHBoxLayout()
        det = QPushButton("Detectar chats")
        det.setObjectName("ghost")
        det.clicked.connect(self.detect_chats)
        test = QPushButton("Enviar prueba")
        test.setObjectName("ghost")
        test.clicked.connect(self.test_tg)
        r3.addWidget(det)
        r3.addWidget(test)
        r3.addStretch(1)
        v.addLayout(r3)
        self.found = QHBoxLayout()
        self.found.setSpacing(8)
        v.addLayout(self.found)
        self.tg_msg = QLabel("")
        self.tg_msg.setWordWrap(True)
        v.addWidget(self.tg_msg)
        v.addStretch(1)
        return page

    def _lbl(self, t):
        l = QLabel(t)
        l.setStyleSheet("color:#AEB7D2;font-size:13px;font-weight:500;margin-top:6px;")
        return l

    def _tg_message(self, text, ok):
        self.tg_msg.setObjectName("ok" if ok else "err")
        self.tg_msg.setStyleSheet("")
        self.tg_msg.setText(text)
        self.tg_msg.style().unpolish(self.tg_msg)
        self.tg_msg.style().polish(self.tg_msg)

    def save_token(self):
        self.mon.set_telegram(token=self.tg_token.text())
        self._tg_message("Token guardado.", True)

    def save_chats(self):
        ids = [x for x in self.tg_chats.text().replace(",", " ").split() if x]
        self.mon.set_telegram(chat_ids=ids)
        self._tg_message("Chats guardados.", True)

    def detect_chats(self):
        while self.found.count():
            it = self.found.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self.mon.tg.token:
            self._tg_message("Primero guarda el token del bot.", False)
            return
        chats = dict(self.mon.tg.seen_chats)
        if not chats:
            self._tg_message("Aún no veo mensajes. Escríbele a tu bot y vuelve a presionar en unos segundos.", False)
            return
        self._tg_message("Haz clic en un chat para agregarlo:", True)
        for cid, name in chats.items():
            b = QPushButton(f"{name}  ·  {cid}")
            b.setObjectName("ghost")
            b.clicked.connect(lambda _=False, c=cid: self._add_chat(c))
            self.found.addWidget(b)
        self.found.addStretch(1)

    def _add_chat(self, cid):
        ids = [x for x in self.tg_chats.text().replace(",", " ").split() if x]
        if cid not in ids:
            ids.append(cid)
        self.tg_chats.setText(", ".join(ids))
        self.save_chats()

    def test_tg(self):
        err = self.mon.tg.send_now("✅ Prueba del Monitor Al Aire: las alertas llegarán aquí.")
        self._tg_message("Mensaje de prueba enviado." if not err else f"No se pudo enviar: {err}", not err)

    # ---- Correo ----
    def _page_mail(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(10)
        h1 = QLabel("Alertas por correo")
        h1.setObjectName("h1")
        hint = QLabel("Se manda un correo cuando una estación sale del aire y cuando regresa.\n"
                      "Los destinatarios se ponen en cada estación (pestaña Estaciones).\n"
                      "Con Gmail usa una «contraseña de aplicación», no la contraseña normal.")
        hint.setObjectName("muted")
        v.addWidget(h1)
        v.addWidget(hint)
        c = self.mon.cfg["correo"]
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        self.m_user = QLineEdit(c.get("usuario", ""))
        self.m_user.setPlaceholderText("cuenta@gmail.com")
        self.m_pass = QLineEdit(c.get("contrasena", ""))
        self.m_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.m_host = QLineEdit(c.get("servidor", "smtp.gmail.com"))
        self.m_port = QLineEdit(str(c.get("puerto", 587)))
        self.m_from = QLineEdit(c.get("remitente", "Monitor Al Aire"))
        for i, (label, w) in enumerate((("Cuenta que envía", self.m_user), ("Contraseña de aplicación", self.m_pass),
                                        ("Servidor SMTP", self.m_host), ("Puerto", self.m_port),
                                        ("Nombre del remitente", self.m_from))):
            r, col = divmod(i, 2)
            grid.addWidget(self._lbl(label), r * 2, col)
            grid.addWidget(w, r * 2 + 1, col)
        v.addLayout(grid)
        v.addSpacing(6)
        row = QHBoxLayout()
        save = QPushButton("Guardar")
        save.clicked.connect(self.save_mail)
        self.m_test_to = QLineEdit(c.get("usuario", ""))
        self.m_test_to.setPlaceholderText("Enviar prueba a…")
        test = QPushButton("Enviar prueba")
        test.setObjectName("ghost")
        test.clicked.connect(self.test_mail)
        row.addWidget(save)
        row.addSpacing(12)
        row.addWidget(self.m_test_to, 1)
        row.addWidget(test)
        v.addLayout(row)
        self.m_msg = QLabel("")
        self.m_msg.setWordWrap(True)
        v.addWidget(self.m_msg)
        v.addStretch(1)
        return page

    def _mail_message(self, text, ok):
        self.m_msg.setStyleSheet(f"color:{'#2EE59D' if ok else '#FF7A90'};font-size:13px;")
        self.m_msg.setText(text)

    def save_mail(self):
        self.mon.set_correo({"usuario": self.m_user.text(), "contrasena": self.m_pass.text(),
                             "servidor": self.m_host.text(), "puerto": self.m_port.text(),
                             "remitente": self.m_from.text()})
        self._mail_message("Guardado.", True)

    def test_mail(self):
        self.save_mail()
        to = [x for x in self.m_test_to.text().replace(";", ",").replace(" ", ",").split(",") if x]
        if not to:
            self._mail_message("Escribe a qué correo mandar la prueba.", False)
            return
        self._mail_message("Enviando…", True)
        QApplication.processEvents()
        err = self.mon.mail.send_now("✅ Prueba del Monitor Al Aire",
                                     "Este es un correo de prueba. Las alertas llegarán así.", to)
        self._mail_message("Correo de prueba enviado." if not err else f"No se pudo enviar: {err}", not err)

    # ---- Avanzado ----
    def _page_advanced(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(12)
        h1 = QLabel("Avanzado")
        h1.setObjectName("h1")
        hint = QLabel("Ya vienen los valores recomendados. Cámbialos solo si lo necesitas.")
        hint.setObjectName("muted")
        v.addWidget(h1)
        v.addWidget(hint)
        g = self.mon.cfg["general"]
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        fields = [
            ("alerta_tras_segundos", "Avisar tras (segundos sin audio)", 10, 3600, 0),
            ("recuperacion_segundos", "Confirmar regreso (segundos)", 3, 600, 0),
            ("umbral_db", "Umbral de silencio (dB)", -90, -10, 0),
            ("aviso_stream_tras_segundos", "Aviso si cae un solo stream (s, 0 = no)", 0, 7200, 0),
            ("recordatorio_minutos", "Recordatorio mientras siga fuera (min, 0 = no)", 0, 1440, 0),
        ]
        self.adv = {}
        for i, (k, label, lo, hi, dec) in enumerate(fields):
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(dec)
            sb.setValue(float(g.get(k, motor.DEFAULTS[k])))
            sb.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            self.adv[k] = sb
            r, c = divmod(i, 2)
            grid.addWidget(self._lbl(label), r * 2, c)
            grid.addWidget(sb, r * 2 + 1, c)
        v.addLayout(grid)
        v.addSpacing(10)
        self.autostart = QCheckBox("Abrir el monitor automáticamente al iniciar Windows")
        self.autostart.setStyleSheet("font-size:14px;spacing:10px;")
        self.autostart.setChecked(autostart_enabled())
        self.autostart.toggled.connect(self.toggle_autostart)
        v.addWidget(self.autostart)
        row = QHBoxLayout()
        save = QPushButton("Guardar")
        save.clicked.connect(self.save_adv)
        self.adv_msg = QLabel("")
        self.adv_msg.setObjectName("ok")
        row.addWidget(save)
        row.addWidget(self.adv_msg)
        row.addStretch(1)
        v.addSpacing(8)
        v.addLayout(row)
        v.addStretch(1)
        return page

    def toggle_autostart(self, on: bool):
        err = set_autostart(on)
        if err:
            self.adv_msg.setText(f"No se pudo: {err}")
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not on)
            self.autostart.blockSignals(False)
        else:
            self.adv_msg.setText("Se abrirá al iniciar Windows." if on else "Ya no se abrirá al iniciar Windows.")

    def save_adv(self):
        self.mon.set_general({k: sb.value() for k, sb in self.adv.items()})
        self.adv_msg.setText("Guardado.")


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def startup_shortcut() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", f"{APP_NAME}.lnk")


def autostart_enabled() -> bool:
    return os.name == "nt" and os.path.exists(startup_shortcut())


def set_autostart(enable: bool) -> str | None:
    """Crea o quita el acceso directo en la carpeta Inicio de Windows."""
    if os.name != "nt":
        return "Solo disponible en Windows"
    lnk = startup_shortcut()
    if not enable:
        try:
            if os.path.exists(lnk):
                os.remove(lnk)
            return None
        except OSError as e:
            return str(e)
    if getattr(sys, "frozen", False):
        target, args = sys.executable, ""
    else:
        target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        args = f'"{os.path.abspath(__file__)}"'
    icon = os.path.join(motor.APP_DIR, "icono.ico")
    q = lambda v: "'" + v.replace("'", "''") + "'"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
          f"$s.TargetPath={q(target)};$s.Arguments={q(args)};"
          f"$s.WorkingDirectory={q(motor.ROOT_DIR)};$s.IconLocation={q(icon)};$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return None
    except Exception as e:
        return str(e)


def load_fonts():
    fdir = os.path.join(motor.APP_DIR, "fonts")
    if os.path.isdir(fdir):
        for f in os.listdir(fdir):
            if f.endswith(".ttf"):
                QFontDatabase.addApplicationFont(os.path.join(fdir, f))


def dark_title_bar(win: QWidget):
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / anteriores)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MonitorAlAire")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # Una sola instancia: si ya está abierto, solo mostrar su ventana
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    if "--autoprueba" in sys.argv:
        os.environ["MONITOR_SIN_ALERTAS"] = "1"
    load_fonts()
    app.setFont(font(14))
    app.setStyleSheet(QSS)

    mon = motor.Monitor()
    mon.start()
    win = MainWindow(mon)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_window()))
    dark_title_bar(win)
    win.show()
    # Prueba automática (la usa la compilación en GitHub): guarda una captura y sale
    if "--autoprueba" in sys.argv:
        out = sys.argv[sys.argv.index("--autoprueba") + 1]
        def finish():
            win.grab().save(out)
            import json
            with open(os.path.splitext(out)[0] + "_estado.json", "w", encoding="utf-8") as f:
                json.dump(mon.status(), f, ensure_ascii=False, indent=2)
            mon.shutdown()
            os._exit(0)
        espera = int(sys.argv[sys.argv.index("--autoprueba") + 2]) if len(sys.argv) > sys.argv.index("--autoprueba") + 2 else 6
        QTimer.singleShot(espera * 1000, finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main() or 0)
