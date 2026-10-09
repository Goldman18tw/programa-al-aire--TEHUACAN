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

Este módulo es el motor (sin interfaz). La ventana está en monitor_app.py.
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Rutas y configuración
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    # .exe: todo junto al ejecutable
    APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    ROOT_DIR = os.path.dirname(sys.executable)
else:
    # versión portátil: <carpeta>/app/motor.py, con <carpeta>/ffmpeg y <carpeta>/python
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "datos")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config_monitor.json")
LOG_FILE = os.path.join(DATA_DIR, "monitor_aire.log")
INITIAL_FILE = os.path.join(APP_DIR, "estaciones_iniciales.json")

DEFAULTS = {
    "umbral_db": -60.0,             # por debajo de esto se considera silencio (silencio real < -70)
    "alerta_tras_segundos": 90,     # estación sin audio en TODOS sus streams
    "recuperacion_segundos": 15,    # audio continuo para declarar "de vuelta al aire"
    "aviso_stream_tras_segundos": 300,  # aviso si solo UN stream está caído (0 = no avisar)
    "recordatorio_minutos": 30,     # recordatorio mientras siga fuera (0 = no)
}

SAMPLE_RATE = 8000          # suficiente para medir nivel y muy ligero
WINDOW_BYTES = SAMPLE_RATE * 2 // 2  # medio segundo de audio s16le mono
NO_DATA_RESTART_SECS = 20   # si FFmpeg no entrega datos en este tiempo, se reinicia
MAX_BACKOFF = 30
NET_CHECK_INTERVAL = 15
NET_CHECK_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443), ("api.telegram.org", 443)]
SLEEP_JUMP_SECS = 30        # si el bucle se "salta" más que esto, la PC se suspendió
UI_BAD_SECS = 10            # en la vista, un stream se marca mal tras este silencio

# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

log = logging.getLogger("monitor_aire")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)
if sys.stdout and not getattr(sys, "frozen", False):
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_fmt)
    log.addHandler(_ch)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def load_config() -> dict:
    cfg = {}
    # La primera vez se cargan las estaciones que trae el programa
    path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else INITIAL_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception as e:
            log.error(f"No pude leer {path}: {e}")
    general = dict(DEFAULTS)
    general.update(cfg.get("general", {}))
    cfg["general"] = general
    tg = cfg.setdefault("telegram", {})
    tg.setdefault("token", "")
    tg.setdefault("chat_ids", [])
    cfg.setdefault("estaciones", [])
    for st in cfg["estaciones"]:
        st.setdefault("id", new_id())
        st.setdefault("logo", "")
        st.setdefault("streams", [])
        for i, s in enumerate(st["streams"]):
            if isinstance(s, str):
                st["streams"][i] = s = {"url": s}
            s.setdefault("id", new_id())
            s.setdefault("nombre", f"Stream {i + 1}")
    return cfg


def save_config(cfg: dict):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def ffmpeg_path() -> str | None:
    for base in (os.path.join(ROOT_DIR, "ffmpeg"), ROOT_DIR, APP_DIR):
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return shutil.which("ffmpeg")


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
    si el internet falla unos segundos). Token y chats se pueden cambiar en vivo."""

    def __init__(self):
        self.token = ""
        self.chat_ids: list[str] = []
        self.q: queue.Queue = queue.Queue()
        self.seen_chats: dict[str, str] = {}  # chat_id -> nombre (para "detectar")
        self.command_handler = None
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()

    def configure(self, token: str, chat_ids: list):
        token = (token or "").strip()
        if token != self.token:
            self.seen_chats = {}
        self.token = token
        self.chat_ids = [str(c).strip() for c in chat_ids if str(c).strip()]

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)

    def _api(self, method: str, params: dict, timeout: float = 15, token: str | None = None):
        url = f"https://api.telegram.org/bot{token or self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def send(self, text: str, chat_id: str | None = None):
        log.info("TELEGRAM: " + text.replace("\n", " | "))
        if not self.token:
            return
        targets = [str(chat_id)] if chat_id else self.chat_ids
        for cid in targets:
            self.q.put((cid, text, time.time()))

    def send_now(self, text: str) -> str | None:
        """Envío directo (para el botón "Probar"). Devuelve error o None."""
        if not self.enabled:
            return "Falta el token o el chat"
        try:
            for cid in self.chat_ids:
                self._api("sendMessage", {"chat_id": cid, "text": text, "parse_mode": "HTML"})
            return None
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode()).get("description", str(e))
            except Exception:
                return str(e)
        except Exception as e:
            return str(e)

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
                except urllib.error.HTTPError as e:
                    # 4xx = token o chat incorrectos: reintentar no sirve (salvo 429 = esperar)
                    if e.code != 429 and 400 <= e.code < 500:
                        log.error(f"Telegram rechazó el mensaje (HTTP {e.code}); revisa token y chat")
                        break
                    log.warning(f"Telegram: error enviando ({e}); reintento en {delay}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                except Exception as e:
                    if time.time() - created > 6 * 3600 or not self.token:
                        log.error(f"Telegram: se descarta mensaje ({e})")
                        break
                    log.warning(f"Telegram: error enviando ({e}); reintento en {delay}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)

    def _poller(self):
        """Escucha comandos (/estado) y recuerda qué chats le escriben al bot."""
        offset = 0
        token_used = ""
        while True:
            token = self.token
            if not token:
                time.sleep(3)
                continue
            if token != token_used:
                offset, token_used = 0, token
            try:
                res = self._api("getUpdates", {"offset": offset, "timeout": 25},
                                timeout=35, token=token)
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    chat = msg.get("chat") or {}
                    cid = str(chat.get("id", ""))
                    if not cid:
                        continue
                    self.seen_chats[cid] = (chat.get("title") or
                                            " ".join(filter(None, [chat.get("first_name"),
                                                                   chat.get("last_name")])) or
                                            chat.get("username") or cid)
                    text = (msg.get("text") or "").strip().lower()
                    if cid in self.chat_ids and text.startswith("/") and self.command_handler:
                        reply = self.command_handler(text.split()[0].split("@")[0])
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
    def __init__(self, station: str, sid: str, label: str, url: str, threshold_db: float):
        self.station = station
        self.id = sid
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
        self.ever_data = False
        self.proc: subprocess.Popen | None = None
        self.stopped = False
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
        threading.Thread(target=self._run, args=(ffmpeg,), daemon=True).start()
        threading.Thread(target=self._stall_guard, daemon=True).start()

    def stop(self):
        self.stopped = True
        self._kill()

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
        while not self.stopped:
            got_data = False
            try:
                self.proc = subprocess.Popen(
                    self._cmd(ffmpeg), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=flags, bufsize=0)
                buf = b""
                while not self.stopped:
                    chunk = self.proc.stdout.read(4000)
                    if not chunk:
                        break
                    if not got_data:
                        got_data = True
                        log.info(f"[{self.name}] conectado")
                    with self.lock:
                        self.last_data = time.monotonic()
                        self.connected = True
                        self.ever_data = True
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
            if self.stopped:
                break
            if got_data:
                backoff = 2
                log.info(f"[{self.name}] conexión terminada; reconectando")
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
        while not self.stopped:
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

    def state(self, t: float) -> str:
        last_data, last_sound, _, _, connected = self.snapshot()
        if not self.ever_data:
            return "conectando" if t - last_sound < 20 else "sin_conexion"
        if not connected or t - last_data > 5:
            return "sin_conexion"
        return "audio" if t - last_sound < 5 else "silencio"

    def describe(self, t: float) -> str:
        silent_for = t - self.snapshot()[1]
        st = self.state(t)
        lbl = html.escape(self.label)
        if st == "audio":
            return f"🟢 {lbl}: audio ({self.level_db:.0f} dB)"
        if st == "silencio":
            return f"🟠 {lbl}: silencio ({human(silent_for)})"
        if st == "conectando":
            return f"⚪ {lbl}: conectando"
        return f"🔴 {lbl}: sin conexión ({human(silent_for)})"

    def to_json(self, t: float) -> dict:
        _, last_sound, _, level, _ = self.snapshot()
        st = self.state(t)
        return {"id": self.id, "nombre": self.label, "url": self.url, "estado": st,
                "nivel": round(level, 1) if st == "audio" else -100,
                "sin_audio_seg": int(max(0, t - last_sound))}


# ---------------------------------------------------------------------------
# Estación (agrupa varios streams)
# ---------------------------------------------------------------------------

class Station:
    def __init__(self, sid: str):
        self.id = sid
        self.name = ""
        self.logo = ""
        self.streams: list[StreamMonitor] = []
        self.off_air = False
        self.off_since_mono = 0.0
        self.off_since_wall = ""
        self.off_since_epoch = 0.0
        self.last_reminder = 0.0

    def apply(self, cfg: dict, general: dict, ffmpeg: str | None):
        """Aplica la configuración conservando los streams que no cambiaron."""
        self.name = cfg["nombre"]
        self.logo = cfg.get("logo", "")
        g = dict(general)
        g.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self.alert_after = float(g["alerta_tras_segundos"])
        self.recover_after = float(g["recuperacion_segundos"])
        self.stream_alert_after = float(g["aviso_stream_tras_segundos"])
        self.reminder = float(g["recordatorio_minutos"]) * 60
        old = {s.id: s for s in self.streams}
        new_list = []
        for i, s in enumerate(cfg.get("streams", []), 1):
            url = (s.get("url") or "").strip()
            if not url:
                continue
            thr = float(s.get("umbral_db", g["umbral_db"]))
            label = s.get("nombre") or f"Stream {i}"
            cur = old.pop(s["id"], None)
            if cur and cur.url == url:
                cur.label, cur.station, cur.threshold = label, self.name, thr
                new_list.append(cur)
                continue
            if cur:
                cur.stop()
            sm = StreamMonitor(self.name, s["id"], label, url, thr)
            log.info(f"Monitoreando {sm.name}: {url}")
            if ffmpeg:
                sm.start(ffmpeg)
            new_list.append(sm)
        for s in old.values():
            s.stop()
        self.streams = new_list
        if not self.streams:
            self.off_air = False

    def stop(self):
        for s in self.streams:
            s.stop()

    def evaluate(self, t: float, tg: Telegram):
        if not self.streams:
            return
        snaps = [s.snapshot() for s in self.streams]
        # Tiempo desde que CUALQUIER stream tuvo audio
        silent_all = min(t - sn[1] for sn in snaps)

        if not self.off_air:
            if silent_all >= self.alert_after:
                self.off_air = True
                self.off_since_mono = t - silent_all
                self.off_since_epoch = time.time() - silent_all
                self.off_since_wall = time.strftime("%H:%M:%S", time.localtime(self.off_since_epoch))
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
        if not self.streams:
            return f"⚪ <b>{html.escape(self.name)}</b> sin streams"
        head = (f"🔴 <b>{html.escape(self.name)}</b> FUERA ({human(t - self.off_since_mono)})"
                if self.off_air else f"🟢 <b>{html.escape(self.name)}</b> al aire")
        return head + "\n" + self.details(t)

    def to_json(self, t: float) -> dict:
        streams = [s.to_json(t) for s in self.streams]
        states = [s["estado"] for s in streams]
        if not streams:
            estado = "sin_streams"
        elif self.off_air:
            estado = "fuera"
        elif all(e == "conectando" for e in states):
            estado = "conectando"
        elif "audio" not in states:
            estado = "verificando"  # sin audio, aún no se confirma la caída
        elif any(e != "audio" and s["sin_audio_seg"] >= UI_BAD_SECS
                 for e, s in zip(states, streams)):
            estado = "parcial"
        else:
            estado = "aire"
        return {"id": self.id, "nombre": self.name, "logo": self.logo, "estado": estado,
                "fuera_desde": self.off_since_wall if self.off_air else None,
                "fuera_seg": int(t - self.off_since_mono) if self.off_air else 0,
                "streams": streams}


# ---------------------------------------------------------------------------
# Motor
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


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = load_config()
        self.ffmpeg = ffmpeg_path()
        if not self.ffmpeg:
            log.error("No se encontró ffmpeg; no se puede medir el audio.")
        self.tg = Telegram()
        self.tg.command_handler = self.handle_command
        self.net = NetChecker()
        self.stations: dict[str, Station] = {}
        self.net_down_since = None
        self.apply()

    # ---- configuración ----
    def apply(self):
        with self.lock:
            t = self.cfg["telegram"]
            self.tg.configure(t.get("token", ""), t.get("chat_ids", []))
            seen = set()
            for sc in self.cfg["estaciones"]:
                st = self.stations.get(sc["id"]) or Station(sc["id"])
                st.apply(sc, self.cfg["general"], self.ffmpeg)
                self.stations[sc["id"]] = st
                seen.add(sc["id"])
            for sid in list(self.stations):
                if sid not in seen:
                    self.stations.pop(sid).stop()
            # Mantener el orden de la configuración
            self.stations = {sc["id"]: self.stations[sc["id"]] for sc in self.cfg["estaciones"]}

    def save(self):
        with self.lock:
            save_config(self.cfg)
            self.apply()

    def find_station_cfg(self, sid: str) -> dict | None:
        return next((s for s in self.cfg["estaciones"] if s["id"] == sid), None)

    def add_station(self, nombre: str) -> dict:
        with self.lock:
            st = {"id": new_id(), "nombre": nombre.strip(), "streams": []}
            self.cfg["estaciones"].append(st)
            self.save()
            return st

    def rename_station(self, sid: str, nombre: str):
        with self.lock:
            st = self.find_station_cfg(sid)
            if st and nombre.strip():
                st["nombre"] = nombre.strip()
                self.save()

    def set_station_logo(self, sid: str, logo: str):
        with self.lock:
            st = self.find_station_cfg(sid)
            if st is not None:
                st["logo"] = logo.strip()
                self.save()

    def delete_station(self, sid: str):
        with self.lock:
            self.cfg["estaciones"] = [s for s in self.cfg["estaciones"] if s["id"] != sid]
            self.save()

    def add_stream(self, sid: str, url: str, nombre: str = "") -> dict | None:
        with self.lock:
            st = self.find_station_cfg(sid)
            if not st or not url.strip():
                return None
            s = {"id": new_id(),
                 "nombre": nombre.strip() or f"Stream {len(st['streams']) + 1}",
                 "url": url.strip()}
            st["streams"].append(s)
            self.save()
            return s

    def update_stream(self, sid: str, stream_id: str, nombre: str | None = None,
                      url: str | None = None):
        with self.lock:
            st = self.find_station_cfg(sid) or {}
            s = next((x for x in st.get("streams", []) if x["id"] == stream_id), None)
            if not s:
                return
            if nombre and nombre.strip():
                s["nombre"] = nombre.strip()
            if url and url.strip():
                s["url"] = url.strip()
            self.save()

    def delete_stream(self, sid: str, stream_id: str):
        with self.lock:
            st = self.find_station_cfg(sid)
            if st:
                st["streams"] = [s for s in st["streams"] if s["id"] != stream_id]
                self.save()

    def set_telegram(self, token: str | None = None, chat_ids: list | None = None):
        with self.lock:
            tg = self.cfg["telegram"]
            if token is not None:
                tg["token"] = token.strip()
            if chat_ids is not None:
                tg["chat_ids"] = [str(c).strip() for c in chat_ids if str(c).strip()]
            self.save()

    def set_general(self, values: dict):
        with self.lock:
            for k in DEFAULTS:
                if k in values:
                    try:
                        self.cfg["general"][k] = float(values[k])
                    except (TypeError, ValueError):
                        pass
            self.save()

    def shutdown(self):
        with self.lock:
            for st in self.stations.values():
                st.stop()

    # ---- estado ----
    def status(self) -> dict:
        t = time.monotonic()
        with self.lock:
            sts = [st.to_json(t) for st in self.stations.values()]
        return {"hora": time.strftime("%H:%M:%S"), "internet": self.net.ok,
                "ffmpeg": bool(self.ffmpeg), "telegram": self.tg.enabled,
                "estaciones": sts}

    def handle_command(self, cmd: str) -> str | None:
        t = time.monotonic()
        if cmd in ("/estado", "/status", "/start"):
            with self.lock:
                body = "\n\n".join(st.status_text(t) for st in self.stations.values())
            return "📻 <b>Estado</b> " + now_str() + "\n\n" + (body or "Sin estaciones")
        if cmd in ("/ayuda", "/help"):
            return "/estado - estado de todas las estaciones"
        return None

    # ---- bucle principal ----
    def start(self):
        names = ", ".join(html.escape(s.name) for s in self.stations.values())
        self.tg.send("▶️ Monitor iniciado" + (f": {names}" if names else ""))
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        last_tick = time.monotonic()
        while True:
            time.sleep(1)
            t = time.monotonic()
            with self.lock:
                stations = list(self.stations.values())

            # PC suspendida / bucle congelado: dar nuevo periodo de gracia
            if t - last_tick > SLEEP_JUMP_SECS:
                log.warning(f"Salto de {t - last_tick:.0f}s (¿suspensión?); periodo de gracia")
                for st in stations:
                    for s in st.streams:
                        s.regrace()
            last_tick = t

            # Sin internet en el monitor: no alertar
            if not self.net.ok:
                if self.net_down_since is None:
                    self.net_down_since = t
                    log.warning("El monitor no tiene internet; alertas en pausa")
                continue
            if self.net_down_since is not None:
                down = t - self.net_down_since
                self.net_down_since = None
                for st in stations:
                    for s in st.streams:
                        s.regrace()
                self.tg.send(f"🌐 El monitor estuvo sin internet {human(down)}. "
                             f"Durante ese tiempo no se pudo vigilar las estaciones.")

            for st in stations:
                try:
                    st.evaluate(t, self.tg)
                except Exception as e:
                    log.exception(f"[{st.name}] error evaluando: {e}")
