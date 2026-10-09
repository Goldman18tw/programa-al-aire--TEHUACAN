"""
Monitor "Al Aire" - vigila los streams de las estaciones y avisa por Telegram.

Reglas para evitar falsas alertas:
  * Una estación se considera FUERA DEL AIRE solo si TODOS sus streams llevan
    `alerta_tras_segundos` sin audio (silencio o sin conexión).
  * El estado se calcula cada segundo a partir del audio medido, no de mensajes
    de FFmpeg; por eso la recuperación siempre se detecta (no se "queda pegado").
  * Para declarar que volvió al aire hace falta audio continuo durante
    `recuperacion_segundos` (evita rebotes).
  * Si el propio monitor se queda sin internet (o la PC se suspende), no se
    alerta; al volver se da un nuevo periodo de gracia a todos los streams.

Solo usa la biblioteca estándar de Python + ffmpeg.
"""

import array
import html
import json
import logging
import math
import operator
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Rutas y configuración
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config_monitor.json")
LOG_FILE = os.path.join(BASE_DIR, "monitor_aire.log")

DEFAULTS = {
    "umbral_db": -50.0,             # por debajo de esto se considera silencio
    "alerta_tras_segundos": 90,     # estación sin audio en TODOS sus streams
    "recuperacion_segundos": 15,    # audio continuo para declarar "de vuelta al aire"
    "aviso_stream_tras_segundos": 300,  # aviso si solo UN stream está caído (0 = no avisar)
    "recordatorio_minutos": 30,     # recordatorio mientras siga fuera (0 = no)
}

SAMPLE_RATE = 8000          # suficiente para medir nivel y muy ligero
WINDOW_BYTES = SAMPLE_RATE * 2  # 1 segundo de audio s16le mono
NO_DATA_RESTART_SECS = 20   # si FFmpeg no entrega datos en este tiempo, se reinicia
MAX_BACKOFF = 30
NET_CHECK_INTERVAL = 15
NET_CHECK_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443), ("api.telegram.org", 443)]
SLEEP_JUMP_SECS = 30        # si el bucle se "salta" más que esto, la PC se suspendió

# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

log = logging.getLogger("monitor_aire")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
log.addHandler(_ch)


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        log.error(f"No existe {CONFIG_FILE}. Copia config_monitor.ejemplo.json "
                  f"a config_monitor.json y llénalo.")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    general = dict(DEFAULTS)
    general.update(cfg.get("general", {}))
    cfg["general"] = general
    if not cfg.get("estaciones"):
        log.error("La configuración no tiene estaciones.")
        sys.exit(1)
    return cfg


def ffmpeg_path() -> str:
    for base in (BASE_DIR, getattr(sys, "_MEIPASS", BASE_DIR)):
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    found = shutil.which("ffmpeg")
    if not found:
        log.error("No se encontró ffmpeg. Ponlo junto al programa o en el PATH.")
        sys.exit(1)
    return found


def human(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    """Envía mensajes en un hilo aparte, con reintentos (no se pierden alertas
    si el internet falla unos segundos)."""

    def __init__(self, token: str, chat_ids: list):
        self.token = token
        self.chat_ids = [str(c) for c in chat_ids]
        self.q: queue.Queue = queue.Queue()
        self.enabled = bool(token and self.chat_ids)
        if not self.enabled:
            log.warning("Telegram no configurado: solo se registrará en el log.")
        threading.Thread(target=self._sender, daemon=True).start()

    def _api(self, method: str, params: dict, timeout: float = 15):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def send(self, text: str, chat_id: str | None = None):
        log.info("TELEGRAM: " + text.replace("\n", " | "))
        if not self.enabled:
            return
        targets = [str(chat_id)] if chat_id else self.chat_ids
        for cid in targets:
            self.q.put((cid, text, time.time()))

    def _sender(self):
        while True:
            cid, text, created = self.q.get()
            delay = 2
            while True:
                try:
                    self._api("sendMessage", {"chat_id": cid, "text": text,
                                              "parse_mode": "HTML",
                                              "disable_web_page_preview": "true"})
                    break
                except Exception as e:
                    if time.time() - created > 6 * 3600:
                        log.error(f"Telegram: se descarta mensaje viejo ({e})")
                        break
                    log.warning(f"Telegram: error enviando ({e}); reintento en {delay}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)

    def poll_commands(self, handler):
        """Escucha comandos (/estado) de los chats autorizados."""
        if not self.enabled:
            return
        offset = 0
        while True:
            try:
                res = self._api("getUpdates", {"offset": offset, "timeout": 50}, timeout=60)
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    cid = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip().lower()
                    if cid in self.chat_ids and text.startswith("/"):
                        reply = handler(text.split()[0].split("@")[0])
                        if reply:
                            self.send(reply, chat_id=cid)
            except Exception as e:
                log.debug(f"Telegram getUpdates: {e}")
                time.sleep(10)


# ---------------------------------------------------------------------------
# Monitoreo de un stream
# ---------------------------------------------------------------------------

def rms_db(chunk: bytes) -> float:
    a = array.array("h")
    a.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if sys.byteorder == "big":
        a.byteswap()
    if not a:
        return -100.0
    ms = sum(map(operator.mul, a, a)) / len(a)
    rms = math.sqrt(ms) / 32768.0
    return 20.0 * math.log10(max(rms, 1e-5))


class StreamMonitor:
    def __init__(self, station: str, label: str, url: str, threshold_db: float):
        self.station = station
        self.label = label
        self.url = url
        self.threshold = threshold_db
        self.lock = threading.Lock()
        t = time.monotonic()
        # Arrancamos "como si hubiera audio" para dar periodo de gracia inicial.
        self.last_data = t
        self.last_sound = t
        self.sound_run_start = t
        self.level_db = -100.0
        self.connected = False
        self.proc: subprocess.Popen | None = None
        self.stop = False
        # Estado para avisos de stream individual
        self.bad_alerted = False

    @property
    def name(self) -> str:
        return f"{self.station} / {self.label}"

    def regrace(self):
        """Nuevo periodo de gracia (tras corte de internet del monitor o suspensión)."""
        with self.lock:
            t = time.monotonic()
            self.last_data = max(self.last_data, t)
            self.last_sound = max(self.last_sound, t)
            self.sound_run_start = t

    def snapshot(self):
        with self.lock:
            return (self.last_data, self.last_sound, self.sound_run_start,
                    self.level_db, self.connected)

    def start(self, ffmpeg: str):
        threading.Thread(target=self._run, args=(ffmpeg,), daemon=True,
                         name=f"ff-{self.name}").start()
        threading.Thread(target=self._stall_guard, daemon=True).start()

    def _cmd(self, ffmpeg: str):
        return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-user_agent", "Mozilla/5.0 (MonitorAlAire)",
                "-rw_timeout", "15000000",
                "-i", self.url,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-f", "s16le", "pipe:1"]

    def _run(self, ffmpeg: str):
        backoff = 2
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        while not self.stop:
            got_data = False
            try:
                self.proc = subprocess.Popen(
                    self._cmd(ffmpeg), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=flags, bufsize=0)
                buf = b""
                while True:
                    chunk = self.proc.stdout.read(4000)
                    if not chunk:
                        break
                    if not got_data:
                        got_data = True
                        log.info(f"[{self.name}] conectado")
                    with self.lock:
                        self.last_data = time.monotonic()
                        self.connected = True
                    buf += chunk
                    while len(buf) >= WINDOW_BYTES:
                        win, buf = buf[:WINDOW_BYTES], buf[WINDOW_BYTES:]
                        self._process_window(win)
            except Exception as e:
                log.warning(f"[{self.name}] error en ffmpeg: {e}")
            finally:
                self._kill()
                with self.lock:
                    self.connected = False
            if self.stop:
                break
            if got_data:
                backoff = 2
                log.info(f"[{self.name}] conexión terminada; reconectando")
            else:
                log.debug(f"[{self.name}] no se pudo conectar; reintento en {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _process_window(self, win: bytes):
        db = rms_db(win)
        t = time.monotonic()
        with self.lock:
            self.level_db = db
            if db > self.threshold:
                # Un hueco > 5 s reinicia la racha de audio continuo
                if t - self.last_sound > 5:
                    self.sound_run_start = t
                self.last_sound = t

    def _stall_guard(self):
        """Si FFmpeg se queda colgado sin entregar datos, lo mata para reconectar."""
        while not self.stop:
            time.sleep(5)
            with self.lock:
                stalled = self.connected and time.monotonic() - self.last_data > NO_DATA_RESTART_SECS
            if stalled:
                log.warning(f"[{self.name}] sin datos {NO_DATA_RESTART_SECS}s; reiniciando ffmpeg")
                self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def describe(self, t: float) -> str:
        last_data, last_sound, _, level, connected = self.snapshot()
        silent_for = t - last_sound
        if silent_for < 5:
            return f"🟢 {html.escape(self.label)}: audio ({level:.0f} dB)"
        if not connected or t - last_data > 5:
            return f"🔴 {html.escape(self.label)}: sin conexión ({human(silent_for)})"
        return f"🟠 {html.escape(self.label)}: silencio ({level:.0f} dB, {human(silent_for)})"


# ---------------------------------------------------------------------------
# Estación (agrupa varios streams)
# ---------------------------------------------------------------------------

class Station:
    def __init__(self, cfg: dict, general: dict):
        self.name = cfg["nombre"]
        g = dict(general)
        g.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self.alert_after = float(g["alerta_tras_segundos"])
        self.recover_after = float(g["recuperacion_segundos"])
        self.stream_alert_after = float(g["aviso_stream_tras_segundos"])
        self.reminder = float(g["recordatorio_minutos"]) * 60
        self.streams = []
        for i, s in enumerate(cfg["streams"], 1):
            if isinstance(s, str):
                s = {"url": s}
            self.streams.append(StreamMonitor(
                self.name, s.get("nombre", f"Stream {i}"), s["url"],
                float(s.get("umbral_db", g["umbral_db"]))))
        self.off_air = False
        self.off_since_mono = 0.0
        self.off_since_wall = ""
        self.last_reminder = 0.0

    def evaluate(self, t: float, tg: Telegram):
        snaps = [s.snapshot() for s in self.streams]
        # Tiempo desde que CUALQUIER stream tuvo audio
        silent_all = min(t - sn[1] for sn in snaps)

        if not self.off_air:
            if silent_all >= self.alert_after:
                self.off_air = True
                self.off_since_mono = t - silent_all
                self.off_since_wall = time.strftime(
                    "%H:%M:%S", time.localtime(time.time() - silent_all))
                self.last_reminder = t
                tg.send(f"🚨 <b>{html.escape(self.name)} FUERA DEL AIRE</b>\n"
                        f"Sin audio en ningún stream desde las {self.off_since_wall} "
                        f"({human(silent_all)}).\n\n" + self.details(t))
        else:
            # ¿Algún stream tiene audio continuo suficiente?
            recovered = any(t - sn[1] < 5 and t - sn[2] >= self.recover_after for sn in snaps)
            if recovered:
                dur = t - self.off_since_mono
                self.off_air = False
                # Los streams que sigan mal ya aparecen en este mensaje: no repetir aviso
                for s, sn in zip(self.streams, snaps):
                    s.bad_alerted = t - sn[1] >= 5
                tg.send(f"✅ <b>{html.escape(self.name)} DE VUELTA AL AIRE</b>\n"
                        f"Estuvo fuera {human(dur)} (desde las {self.off_since_wall}).\n\n"
                        + self.details(t))
            elif self.reminder and t - self.last_reminder >= self.reminder:
                self.last_reminder = t
                tg.send(f"⏰ <b>{html.escape(self.name)}</b> sigue fuera del aire "
                        f"({human(t - self.off_since_mono)}).\n\n" + self.details(t))

        # Avisos de un solo stream caído (la estación sigue al aire por otro)
        if self.stream_alert_after > 0 and len(self.streams) > 1:
            for s, sn in zip(self.streams, snaps):
                bad_for = t - sn[1]
                if not s.bad_alerted and not self.off_air and bad_for >= self.stream_alert_after:
                    s.bad_alerted = True
                    tg.send(f"⚠️ <b>{html.escape(s.name)}</b> sin audio desde hace "
                            f"{human(bad_for)}. La estación sigue al aire por otro stream.\n"
                            + s.describe(t))
                elif s.bad_alerted and not self.off_air and t - sn[1] < 5 and t - sn[2] >= self.recover_after:
                    s.bad_alerted = False
                    tg.send(f"👍 <b>{html.escape(s.name)}</b> recuperado.")

    def details(self, t: float) -> str:
        return "\n".join(s.describe(t) for s in self.streams)

    def status_text(self, t: float) -> str:
        head = (f"🔴 <b>{html.escape(self.name)}</b> FUERA ({human(t - self.off_since_mono)})"
                if self.off_air else f"🟢 <b>{html.escape(self.name)}</b> al aire")
        return head + "\n" + self.details(t)


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

class NetChecker:
    """Comprueba si el propio monitor tiene internet."""

    def __init__(self):
        self.ok = True
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _check() -> bool:
        for host, port in NET_CHECK_HOSTS:
            try:
                with socket.create_connection((host, port), timeout=4):
                    return True
            except OSError:
                continue
        return False

    def _loop(self):
        fails = 0
        while True:
            if self._check():
                fails = 0
                self.ok = True
            else:
                fails += 1
                if fails >= 2:
                    self.ok = False
            time.sleep(NET_CHECK_INTERVAL)


def main():
    cfg = load_config()
    tg_cfg = cfg.get("telegram", {})
    tg = Telegram(tg_cfg.get("token", ""), tg_cfg.get("chat_ids", []))
    ffmpeg = ffmpeg_path()
    stations = [Station(s, cfg["general"]) for s in cfg["estaciones"]]
    net = NetChecker()

    for st in stations:
        for s in st.streams:
            log.info(f"Monitoreando {s.name}: {s.url}")
            s.start(ffmpeg)

    def handle_command(cmd: str) -> str | None:
        t = time.monotonic()
        if cmd in ("/estado", "/status", "/start"):
            return "📻 <b>Estado</b> " + now_str() + "\n\n" + \
                   "\n\n".join(st.status_text(t) for st in stations)
        if cmd in ("/ayuda", "/help"):
            return "/estado - estado de todas las estaciones"
        return None

    threading.Thread(target=tg.poll_commands, args=(handle_command,), daemon=True).start()

    tg.send("▶️ Monitor iniciado: " + ", ".join(html.escape(s.name) for s in stations))

    last_tick = time.monotonic()
    net_down_since = None
    while True:
        time.sleep(1)
        t = time.monotonic()

        # PC suspendida / bucle congelado: dar nuevo periodo de gracia
        if t - last_tick > SLEEP_JUMP_SECS:
            log.warning(f"Salto de {t - last_tick:.0f}s (¿suspensión?); periodo de gracia")
            for st in stations:
                for s in st.streams:
                    s.regrace()
        last_tick = t

        # Sin internet en el monitor: no alertar
        if not net.ok:
            if net_down_since is None:
                net_down_since = t
                log.warning("El monitor no tiene internet; alertas en pausa")
            continue
        if net_down_since is not None:
            down = t - net_down_since
            net_down_since = None
            for st in stations:
                for s in st.streams:
                    s.regrace()
            tg.send(f"🌐 El monitor estuvo sin internet {human(down)}. "
                    f"Durante ese tiempo no se pudo vigilar las estaciones.")

        for st in stations:
            try:
                st.evaluate(t, tg)
            except Exception as e:
                log.exception(f"[{st.name}] error evaluando: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Monitor detenido")
