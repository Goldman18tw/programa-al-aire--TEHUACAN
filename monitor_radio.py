import asyncio
import atexit
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sys
import smtplib
import ssl
import numpy as np
from email.message import EmailMessage
import flet as ft
import flet.canvas as cv

# ------- opcional flet_audio -------
try:
    from flet_audio import Audio as FAudio
    HAS_FLET_AUDIO = True
except Exception:
    FAudio = None
    HAS_FLET_AUDIO = False

# ------- opcional VLC -------
try:
    import vlc  # pip install python-vlc
    HAS_VLC = True
except Exception:
    HAS_VLC = False

from aiohttp import web
from collections import deque
import aiohttp
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.cloud import firestore
import hashlib
from dataclasses import dataclass
import time
import subprocess
if os.name == "nt":
    # Flags fuertes
    _CF = (subprocess.CREATE_NO_WINDOW
           | subprocess.DETACHED_PROCESS
           | subprocess.CREATE_NEW_PROCESS_GROUP)
    _SI = subprocess.STARTUPINFO()
    _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SI.wShowWindow = 0  # SW_HIDE

    # Plan B: ocultar ventanas si alcanzan a aparecer
    import ctypes
    import ctypes.wintypes as wt
    _user32 = ctypes.windll.user32
    _EnumWindows = _user32.EnumWindows
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _IsWindowVisible = _user32.IsWindowVisible
    _ShowWindow = _user32.ShowWindow
    _EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, ctypes.c_void_p)

    def _hide_console_windows_for_pid(pid: int):
        def _cb(hwnd, _):
            p = wt.DWORD()
            _GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and _IsWindowVisible(hwnd):
                _ShowWindow(hwnd, 0)  # SW_HIDE
            return True
        _EnumWindows(_EnumProc(_cb), 0)
else:
    _CF = 0
    _SI = None
    def _hide_console_windows_for_pid(pid: int):
        return


PUBLISH_MIN_SECS = 45  # anti-ráfagas por estación

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8090"))

# Eventos recientes (para GET /events)
EVENTS_MAX = 1000
events_deque = deque(maxlen=EVENTS_MAX)

# Conexiones WS
ws_clients: set[web.WebSocketResponse] = set()

APP_NAME = "On-Air Detection"
CONFIG_FILE = "config_alerta_radio.json"
LOG_FILE = "monitor_radio.log"

# --- Escala de UI para compactar tarjetas (solo visual) ---
UI_SCALE = 0.86  # baja a 0.80 si quieres aún más compacto

PUSH_TOPIC = os.getenv("PUSH_TOPIC", "alerts")
# Fallback directo a FCM (sin Cloud Function)
FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")  # pegas aquí tu "Server key" de Firebase
# --- HTTP v1 (OAuth2 con cuenta de servicio) ---
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "on-air-detector")
SA_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("SERVICE_ACCOUNT_FILE")
SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
# --- FALLBACK por si el proceso no ve las variables de entorno ---
if not PROJECT_ID:
    PROJECT_ID = "on-air-detector"

if not SA_FILE:
    SA_FILE = r"C:\Users\Raul Orozco Alvarez\Desktop\Nueva carpeta (6)\programa al aire  TEHUACAN\on-air-detector-firebase-adminsdk-fbsvc-147d81fe61.json"
PUSH_URL = os.getenv("PUSH_URL", "")
PUSH_SECRET = os.getenv("PUSH_SECRET", "")
# Desactiva Cloud Function si quedó un placeholder
if PUSH_URL and ("tu_proyecto" in PUSH_URL or "us-central1-tu_proyecto" in PUSH_URL):
    PUSH_URL = ""
    PUSH_SECRET = ""

# --- Firestore ---
_FS = None
OFF_AFTER_RETRIES = 5  # aumentado de 3 a 5 para evitar falsas alertas

# === NUEVAS CONSTANTES DE VALIDACIÓN ===
SILENCE_CONFIRMATION_SAMPLES = 3  # cuántas muestras consecutivas bajo umbral para confirmar silencio
MIN_AUDIO_SAMPLES_FOR_RECONNECT = 5  # mínimo de muestras antes de reconectar
AUDIO_GAP_TOLERANCE = 2.5  # segundos de tolerancia antes de considerar gap
LEVEL_HYSTERESIS = 2.0  # dB de histéresis para evitar rebotes en el umbral
MAX_FFMPEG_LIFETIME = 4 * 3600  # reinicio preventivo cada 4h (bajado de 6h)
WATCHDOG_INTERVAL = 2  # intervalo del watchdog en segundos

# === SISTEMA DE VERIFICACIÓN ESTRICTA ANTI-FALSAS ALERTAS ===
GRACE_PERIOD_SECONDS = 300  # 5 minutos - tiempo antes de enviar email
VERIFICATION_INTERVAL = 30  # verificar cada 30 segundos durante periodo de gracia
MIN_DOWNTIME_FOR_ALERT = 300  # 5 minutos mínimo para considerar caída real
RECOVERY_GRACE_SECONDS = 60  # si vuelve por menos de 60 seg, no cancelar alerta
SILENCE_THRESHOLD_DEFAULT = -55.0  # umbral más bajo para evitar falsas alertas con música suave

# === NUEVAS CONSTANTES PARA FUNCIONALIDADES ADICIONALES ===
NETWORK_CHECK_INTERVAL = 30  # segundos entre chequeos de red
NETWORK_CHECK_HOSTS = ["8.8.8.8", "1.1.1.1"]  # hosts para verificar conectividad
AUTO_ADJUST_SAMPLES = 50  # muestras para calcular nivel promedio
AUTO_ADJUST_MARGIN = 5.0  # margen en dB por debajo del promedio
EVENTS_LOG_FILE = "events_history.json"  # archivo de historial de eventos
MAX_EVENTS_IN_FILE = 10000  # máximo de eventos a guardar en archivo

# === RDS / NOW PLAYING ===
RDS_UPDATE_INTERVAL = 15  # segundos entre actualizaciones de RDS
RDS_HISTORY_FILE = "rds_history.json"  # archivo de historial de canciones
MAX_RDS_HISTORY = 50000  # máximo de entradas en historial

# URLs de RDS por estación (key = nombre en minúsculas, value = URL)
RDS_URLS = {
    "puebla": "https://onair.radioapi.io/status/radiobuap/radiobuappuebla/source",
    "tehuacan": "https://onair.radioapi.io/status/radiobuap/radiobuaptehuacan/source",
    "tehuacán": "https://onair.radioapi.io/status/radiobuap/radiobuaptehuacan/source",
    "chignahuapan": "https://onair.radioapi.io/status/radiobuap/radiobuapchignahuapan/source",
}

# Alerta de repetición sospechosa
SUSPICIOUS_REPEAT_COUNT = 5  # si una canción suena 5+ veces en la misma hora
SUSPICIOUS_REPEAT_WINDOW = 3600  # ventana de 1 hora

# === PERFILES DE HORARIO ===
SCHEDULE_PROFILES = {
    "normal": {
        "silence_multiplier": 1.0,
        "db_offset": 0.0,
        "description": "Tolerancia normal"
    },
    "tolerant": {
        "silence_multiplier": 3.0,  # 3x más tiempo antes de alertar
        "db_offset": -5.0,  # 5 dB más tolerante
        "description": "Mayor tolerancia (domingos/lunes 4pm-12am)"
    }
}

def get_current_schedule_profile() -> dict:
    """Determina el perfil de horario actual según día y hora."""
    now = dt.datetime.now()
    day_of_week = now.weekday()  # 0=lunes, 6=domingo
    hour = now.hour
    
    # Domingo (6) o Lunes (0), de 16:00 a 23:59
    if (day_of_week == 6 or day_of_week == 0) and 16 <= hour <= 23:
        return SCHEDULE_PROFILES["tolerant"]
    
    return SCHEDULE_PROFILES["normal"]

@dataclass
class PubState:
    on_air: bool
    name: str
    url: str
    last_ts: float

_last_pub: dict[str, PubState] = {}  # station_id -> PubState

def _fs():
    global _FS
    if _FS is None:
        try:
            creds = service_account.Credentials.from_service_account_file(SA_FILE)
            _FS = firestore.Client(project=PROJECT_ID, credentials=creds)
        except Exception as e:
            logger.error(f"Firestore init error: {e}")
            _FS = None
    return _FS

def _should_publish(station_id: str, name: str, url: str, on_air: bool) -> bool:
    now = time.time()
    prev = _last_pub.get(station_id)
    if prev is None:
        return True  # primera vez

    # Evita ráfagas: si no pasó el mínimo, sólo deja pasar cambios de ON/OFF
    if now - prev.last_ts < PUBLISH_MIN_SECS:
        return prev.on_air != on_air

    # ¿cambió estado o identidad?
    changed = (prev.on_air != on_air) or (prev.name != name) or (prev.url != url)
    return changed


async def fs_write_station_if_changed(ui: "StationMonitor"):
    """Escribe en Firestore sólo si cambió on_air / nombre / url (o es la primera vez)."""
    cli = _fs()
    if not cli:
        return False

    station_id = _station_doc_id(ui.cfg.get("url", ""))
    name = ui.cfg.get("name", "")
    url  = ui.cfg.get("url", "")
    on_air = bool(ui.on_air)

    if not _should_publish(station_id, name, url, on_air):
        return False

    data = station_to_json(ui)
    data["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    data["project_id"] = PROJECT_ID

    try:
        ref = cli.collection("stations").document(station_id)
        # actualizar cache optimista para que un cambio inmediato contrario NO se descarte
        _last_pub[station_id] = PubState(on_air=on_air, name=name, url=url, last_ts=time.time())
        await asyncio.to_thread(ref.set, data, True)  # luego escribimos
        # (opcional) re-escribe el cache por si cambió algo
        _last_pub[station_id] = PubState(on_air=on_air, name=name, url=url, last_ts=time.time())
        logger.info(f"[FS] publicado {station_id} (on_air={on_air})")
        return True
    
    except Exception as e:
        logger.error(f"Firestore write station error: {e}")
        return False

def _station_doc_id(url: str) -> str:
    # Evita / en IDs. Usamos hash estable de la URL (en minúsculas).
    u = (url or "").strip().lower()
    return hashlib.md5(u.encode("utf-8")).hexdigest()

async def fs_write_station(ui: "StationMonitor"):
    """Guarda/actualiza el doc de la estación en Firestore."""
    cli = _fs()
    if not cli:
        return False
    data = station_to_json(ui)
    data["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    data["project_id"] = PROJECT_ID
    try:
        col = cli.collection("stations")
        doc = col.document(_station_doc_id(ui.cfg.get("url", "")))
        # Ejecuta en hilo para no bloquear el loop
        await asyncio.to_thread(doc.set, data, True)
        return True
    except Exception as e:
        logger.error(f"Firestore write station error: {e}")
        return False

async def fs_add_event(evt: dict):
    """Opcional: guarda cada evento en una colección 'events'."""
    cli = _fs()
    if not cli:
        return False
    try:
        evt2 = dict(evt)
        evt2["project_id"] = PROJECT_ID
        evt2["ingested_at"] = dt.datetime.now(dt.UTC).isoformat()
        col = cli.collection("events")
        await asyncio.to_thread(col.add, evt2)
        return True
    except Exception as e:
        logger.error(f"Firestore add event error: {e}")
        return False

async def fs_publish_all(current_stations: list["StationMonitor"]):
    """Empuja todas las estaciones a Firestore (batch)."""
    cli = _fs()
    if not cli:
        return False

    batch = cli.batch()
    now = dt.datetime.now(dt.UTC).isoformat()

    for ui in current_stations:
        station_id = _station_doc_id(ui.cfg.get("url", ""))
        ref = cli.collection("stations").document(station_id)
        data = station_to_json(ui)
        data["updated_at"] = now
        data["project_id"] = PROJECT_ID
        batch.set(ref, data, merge=True)

        # refrescamos cache para que no re-publiquen de inmediato
        _last_pub[station_id] = PubState(
            on_air=bool(ui.on_air), name=ui.cfg.get("name",""), url=ui.cfg.get("url",""), last_ts=time.time()
        )

    try:
        # commit síncrono en hilo aparte
        await asyncio.to_thread(batch.commit)
        logger.info(f"[FS] snapshot completo ({len(current_stations)}) publicado")
        return True
    except Exception as e:
        logger.error(f"Firestore batch commit error: {e}")
        return False


# ---------- LOG ----------
logger = logging.getLogger("monitor")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(handler)
console = logging.StreamHandler(sys.stdout)
console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(console)

# ---------- SMTP ----------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Fallback si no hay variables de entorno (puedes cambiarlos si necesitas)
SMTP_USER = os.getenv("SMTP_USER") or "rboffairdetector@gmail.com"
SMTP_PASS = os.getenv("SMTP_PASS") or "saysuxlgdpcjwmgv"

DEFAULT_FROM = "Off-Air Detector <rboffairdetector@gmail.com>"

# ---------- Defaults ----------
DEFAULT_STATION = {
    "name": "XHBUAP - Puebla",
    "url": "http://148.228.50.35:8000",
    "from": "Off-Air Detector Puebla <rboffairdetector@gmail.com>",
    "recipients": ["rauloa13@gmail.com"],
    "silence_seconds": 9,
    "db_threshold": -55.0,  # Umbral más bajo para evitar falsas alertas con música suave
    "cooldown_seconds": 60,
    "email_body_alert": "La estación '{name}' no transmite audio desde hace más de {seconds} segundos.",
    "email_body_ok":    "La estación volvió al aire tras {duration}.",
}

MAX_HISTORY = 8

# ---------- Utils ----------
def ffmpeg_path():
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    p = os.path.join(base, exe)
    return p if os.path.exists(p) else exe

def ffplay_path():
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    exe = "ffplay.exe" if os.name == "nt" else "ffplay"
    p = os.path.join(base, exe)
    return p if os.path.exists(p) else exe

def _unique_push(lst: list, value: str):
    v = (value or "").strip()
    if not v:
        return
    if v in lst:
        lst.remove(v)
    lst.insert(0, v)
    del lst[MAX_HISTORY:]

def load_cfg():
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            logger.error(f"Error cargando config: {e}")
    # Historial + preferencias por URL
    cfg = {
        "history_names": list(dict.fromkeys(data.get("history_names", [])))[:MAX_HISTORY],
        "history_urls":  list(dict.fromkeys(data.get("history_urls",  [])))[:MAX_HISTORY],
        "stations":      data.get("stations", {}),  # dict: url_lower -> prefs
    }
    return cfg

def save_cfg(cfg):
    safe = {
        "history_names": cfg.get("history_names", [])[:MAX_HISTORY],
        "history_urls":  cfg.get("history_urls",  [])[:MAX_HISTORY],
        "stations":      cfg.get("stations", {}),
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(safe, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando config: {e}")

def human_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60: return f"{s} segundos"
    m, s = divmod(s, 60)
    if m < 60: return f"{m} minutos" + (f" {s} segundos" if s else "")
    h, m = divmod(m, 60)
    if h < 24: return f"{h} horas" + (f" {m} minutos" if m else "")
    d, h = divmod(h, 24)
    return f"{d} días" + (f" {h} horas" if h else "")

def send_mail(station, subject, body):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP_USER / SMTP_PASS no definidos; omitiendo envío.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = station.get("from", DEFAULT_FROM)
    to = station.get("recipients", [])
    if not to:
        logger.warning("Sin destinatarios; omitiendo envío.")
        return False
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        logger.info(f"Correo enviado: {subject}")
        return True
    except Exception as e:
        logger.error(f"Error SMTP: {repr(e)}")
        return False
    
# === NUEVO: util para enviar notificaciones push a tu Function ===
async def send_push_async(title: str, body: str, data: dict | None = None,
                          *, topic: str | None = PUSH_TOPIC, tokens: list[str] | None = None) -> bool:
    """
    Envía un push a la Cloud Function. Si pasas 'tokens' se envía a esos tokens,
    si no, usa el 'topic' (por defecto PUSH_TOPIC).
    No bloquea el loop principal (se usa con asyncio.create_task).
    """
    if not PUSH_URL or not PUSH_SECRET:
        # 1) Intentar HTTP v1 con cuenta de servicio
        if SA_FILE and PROJECT_ID:
            return await _send_push_v1(title, body, data, topic=topic, tokens=tokens)
        # 2) Si tienes Server key legacy, usar el fallback (opcional)
        if FCM_SERVER_KEY:
            return await _send_push_via_fcm(title, body, data, topic=topic, tokens=tokens)
        logger.warning("Sin PUSH_URL/PUSH_SECRET, sin SERVICE ACCOUNT y sin FCM_SERVER_KEY; omitiendo push")
        return False

    payload = {"title": title, "body": body, "data": data or {}}
    if tokens:
        payload["tokens"] = tokens
    elif topic:
        payload["topic"] = topic
    else:
        logger.warning("Sin topic ni tokens para push")
        return False

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(PUSH_URL, json=payload, headers={"x-secret": PUSH_SECRET}) as r:
                if 200 <= r.status < 300:
                    return True
                txt = await r.text()
                logger.error(f"Push error {r.status}: {txt}")
                return False
    except Exception as e:
        logger.error(f"Push exception: {e}")
        return False
# === NUEVO: Fallback directo a FCM (legacy endpoint)
FCM_ENDPOINT = "https://fcm.googleapis.com/fcm/send"

def open_logs_console():
    log_path = os.path.abspath(LOG_FILE)
    if not os.path.exists(log_path):
        open(log_path, "a", encoding="utf-8").close()

    if os.name == "nt":
        cmd = [
            "powershell", "-NoExit", "-Command",
            "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
            f"$Host.UI.RawUI.WindowTitle='Logs On-Air Detection'; "
            f"Get-Content -Path '{log_path}' -Wait -Tail 200"
        ]
        # aquí SÍ queremos una consola nueva para ver los logs:
        subprocess.Popen(cmd)  # sin creationflags/startupinfo para que se vea
    else:
        subprocess.Popen(["sh", "-c", f"tail -n 200 -f '{log_path}'"])


def open_logs_folder():
    p = os.path.abspath(LOG_FILE)
    folder = os.path.dirname(p)
    try:
        if os.name == "nt":
            os.startfile(folder)  # abre el Explorador
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as e:
        logger.error(f"No pude abrir la carpeta de logs: {e}")


# === HTTP v1 con cuenta de servicio ===
def _get_access_token_v1() -> str | None:
    try:
        if not SA_FILE:
            return None
        creds = service_account.Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        logger.error(f"Token v1 error: {e}")
        return None

async def _send_push_v1(title: str, body: str, data: dict | None = None,
                        *, topic: str | None = PUSH_TOPIC, tokens: list[str] | None = None) -> bool:
    if not PROJECT_ID:
        logger.warning("FIREBASE_PROJECT_ID no definido")
        return False

    token = _get_access_token_v1()
    if not token:
        return False

    # v1 exige strings en data
    data = {k: str(v) for k, v in (data or {}).items()}
    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}

    async def _post(payload: dict) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, json=payload, headers=headers) as r:
                    txt = await r.text()
                    if 200 <= r.status < 300:
                        logger.info(f"FCM v1 OK {r.status}: {txt}")  # << verás el "name" del mensaje
                        return True
                    logger.error(f"FCM v1 error {r.status}: {txt}")
                    return False
        except Exception as e:
            logger.error(f"FCM v1 exception: {e}")
            return False


    # Un token por request en v1. Si pasas lista, iteramos.
    if tokens:
        ok_all = True
        for t in tokens:
            payload = {
                "message": {
                    "token": t,
                    "notification": {"title": title, "body": body},
                    "data": data
                }
            }
            ok_all = (await _post(payload)) and ok_all
        return ok_all

    # Topic
    if topic:
        payload = {
            "message": {
                "topic": topic,
                "notification": {"title": title, "body": body},
                "data": data
            }
        }
        return await _post(payload)

    logger.warning("Sin topic ni tokens para FCM v1")
    return False

async def _send_push_via_fcm(title: str, body: str, data: dict | None = None,
                             *, topic: str | None = PUSH_TOPIC, tokens: list[str] | None = None) -> bool:
    if not FCM_SERVER_KEY:
        logger.warning("FCM_SERVER_KEY no definido; no se puede enviar push directo a FCM")
        return False

    # FCM exige strings en 'data'
    data = {k: str(v) for k, v in (data or {}).items()}

    payload: dict = {
        "notification": {"title": title, "body": body},
        "data": data,
        "android": {"priority": "high"},
        "apns": {"headers": {"apns-priority": "10"}}
    }

    if tokens:
        payload["registration_ids"] = tokens
    elif topic:
        payload["to"] = f"/topics/{topic}"
    else:
        logger.warning("Sin topic ni tokens para FCM")
        return False

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {
            "Authorization": f"key={FCM_SERVER_KEY}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(FCM_ENDPOINT, json=payload, headers=headers) as r:
                txt = await r.text()
                if 200 <= r.status < 300:
                    return True
                logger.error(f"FCM error {r.status}: {txt}")
                return False
    except Exception as e:
        logger.error(f"FCM exception: {e}")
        return False

def snack(page: ft.Page, text: str):
    page.snack_bar = ft.SnackBar(ft.Text(text))
    page.snack_bar.open = True
    page.update()

async def check_network_connectivity() -> bool:
    """Verifica si hay conectividad a internet."""
    import socket
    for host in NETWORK_CHECK_HOSTS:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            await asyncio.get_event_loop().run_in_executor(None, sock.connect, (host, 53))
            sock.close()
            return True
        except Exception:
            continue
    return False

def history_button(values: list[str], on_pick):
    items = [ft.PopupMenuItem(text=v, on_click=lambda e, vv=v: on_pick(vv)) for v in values] or \
            [ft.PopupMenuItem(text="(sin historial)", disabled=True)]
    return ft.PopupMenuButton(icon=ft.Icons.ARROW_DROP_DOWN, items=items)

# === GESTIÓN DE HISTORIAL DE EVENTOS ===
def load_events_history() -> list[dict]:
    """Carga el historial de eventos desde archivo."""
    if not os.path.exists(EVENTS_LOG_FILE):
        return []
    try:
        with open(EVENTS_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Error cargando historial de eventos: {e}")
        return []

def save_event_to_history(event: dict):
    """Guarda un evento en el historial."""
    try:
        history = load_events_history()
        history.append(event)
        # Mantener solo los últimos MAX_EVENTS_IN_FILE eventos
        if len(history) > MAX_EVENTS_IN_FILE:
            history = history[-MAX_EVENTS_IN_FILE:]
        with open(EVENTS_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando evento en historial: {e}")

# === GESTIÓN DE HISTORIAL DE RDS ===
def load_rds_history() -> list[dict]:
    """Carga el historial de canciones/RDS."""
    if not os.path.exists(RDS_HISTORY_FILE):
        return []
    try:
        with open(RDS_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Error cargando historial RDS: {e}")
        return []

def save_rds_entry(station_name: str, song_info: str):
    """Guarda una entrada de RDS/canción."""
    try:
        history = load_rds_history()
        entry = {
            "timestamp": dt.datetime.now().isoformat(),
            "station": station_name,
            "song": song_info
        }
        history.append(entry)
        
        # Mantener límite
        if len(history) > MAX_RDS_HISTORY:
            history = history[-MAX_RDS_HISTORY:]
        
        with open(RDS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando RDS: {e}")

def get_song_stats(station_name: str = None) -> dict:
    """Obtiene estadísticas de canciones más sonadas."""
    history = load_rds_history()
    
    # Cargar exclusiones
    exclusions = []
    if os.path.exists("rds_exclusions.json"):
        try:
            with open("rds_exclusions.json", "r", encoding="utf-8") as f:
                exclusions = json.load(f)
        except Exception:
            pass
    
    # Filtrar por estación si se especifica
    if station_name:
        history = [h for h in history if h.get("station") == station_name]
    
    # Contar repeticiones (excluyendo las de la lista)
    song_counts = {}
    song_timestamps = {}
    
    for entry in history:
        song = entry.get("song", "").strip()
        if not song or song == "Unknown" or song in exclusions:
            continue
        
        if song not in song_counts:
            song_counts[song] = 0
            song_timestamps[song] = []
        
        song_counts[song] += 1
        song_timestamps[song].append(entry.get("timestamp"))
    
    # Ordenar por más sonadas
    sorted_songs = sorted(song_counts.items(), key=lambda x: x[1], reverse=True)
    
    return {
        # Devolver TODAS las canciones ordenadas por repeticiones
        "top_songs": sorted_songs,
        "timestamps": song_timestamps,
        "total_tracked": len(history)
    }

def check_suspicious_pattern(song: str, station: str) -> bool:
    """Detecta si una canción tiene patrón sospechoso de repetición.
    
    Patrón sospechoso: Suena 4+ veces en 2+ días diferentes a la misma hora (14:XX, 15:XX, etc.)
    """
    history = load_rds_history()
    
    # Filtrar por canción y estación
    song_entries = [
        h for h in history 
        if h.get("song") == song and h.get("station") == station
    ]
    
    if len(song_entries) < 4:
        return False  # Necesita al menos 4 ocurrencias
    
    # Agrupar por día y hora (redondeada)
    plays_by_day_hour = {}  # {("2025-12-11", 14): [timestamps...]}
    
    for entry in song_entries:
        try:
            ts = dt.datetime.fromisoformat(entry["timestamp"])
            day = ts.date().isoformat()
            hour = ts.hour  # Solo la hora (0-23)
            
            key = (day, hour)
            if key not in plays_by_day_hour:
                plays_by_day_hour[key] = []
            plays_by_day_hour[key].append(ts)
        except Exception:
            continue
    
    # Buscar si hay alguna hora donde suene en 2+ días diferentes
    hours_with_days = {}  # {14: ["2025-12-11", "2025-12-12", ...]}
    
    for (day, hour), timestamps in plays_by_day_hour.items():
        if hour not in hours_with_days:
            hours_with_days[hour] = set()
        hours_with_days[hour].add(day)
    
    # Verificar si alguna hora tiene 2+ días Y 4+ ocurrencias totales en esos días
    for hour, days in hours_with_days.items():
        if len(days) >= 2:  # Al menos 2 días diferentes
            # Contar ocurrencias totales en esa hora
            total_plays = sum(
                len(plays_by_day_hour.get((day, hour), []))
                for day in days
            )
            if total_plays >= 4:
                return True  # Patrón sospechoso detectado
    
    return False

def is_alert_ignored(song: str, station: str) -> bool:
    """Verifica si se ignoró la alerta de esta canción."""
    ignored_file = "rds_alerts_ignored.json"
    if not os.path.exists(ignored_file):
        return False
    try:
        with open(ignored_file, "r", encoding="utf-8") as f:
            ignored = json.load(f)
        key = f"{station}:{song}"
        return key in ignored
    except Exception:
        return False

def ignore_alert(song: str, station: str):
    """Marca una canción para ignorar su alerta."""
    ignored_file = "rds_alerts_ignored.json"
    try:
        if os.path.exists(ignored_file):
            with open(ignored_file, "r", encoding="utf-8") as f:
                ignored = json.load(f)
        else:
            ignored = []
        
        key = f"{station}:{song}"
        if key not in ignored:
            ignored.append(key)
            with open(ignored_file, "w", encoding="utf-8") as f:
                json.dump(ignored, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Alerta ignorada para: {song} en {station}")
    except Exception as e:
        logger.error(f"Error ignorando alerta: {e}")

def get_station_stats(station_url: str, hours: int = 24) -> dict:
    """Obtiene estadísticas de una estación en las últimas X horas."""
    history = load_events_history()
    station_id = station_url.lower()
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(hours=hours)
    
    events = [e for e in history 
              if e.get("station_id") == station_id 
              and dt.datetime.fromisoformat(e["timestamp"]) >= cutoff]
    
    total_downtime = 0.0
    downtime_count = 0
    last_down_time = None
    
    for event in events:
        if event.get("event_type") == "offline":
            last_down_time = dt.datetime.fromisoformat(event["timestamp"])
            downtime_count += 1
        elif event.get("event_type") == "online" and last_down_time:
            online_time = dt.datetime.fromisoformat(event["timestamp"])
            total_downtime += (online_time - last_down_time).total_seconds()
            last_down_time = None
    
    # Si aún está caído, contar hasta ahora
    if last_down_time:
        total_downtime += (now - last_down_time).total_seconds()
    
    total_seconds = hours * 3600
    uptime_percent = ((total_seconds - total_downtime) / total_seconds * 100) if total_seconds > 0 else 100.0
    
    return {
        "uptime_percent": uptime_percent,
        "downtime_count": downtime_count,
        "total_downtime_seconds": total_downtime,
        "events_count": len(events)
    }

def station_to_json(ui: "StationMonitor") -> dict:
    return {
        "id": ui.cfg.get("url", "").lower(),  # usamos URL como id estable
        "name": ui.cfg.get("name", ""),
        "stream_url": _player_friendly_url(ui.cfg.get("url","")),
        "on_air": bool(ui.on_air),
        "level_dbfs": float(ui.current_db),
        "last_change": (ui.silence_start_ts or getattr(ui, "last_audio_ts", dt.datetime.now())).isoformat(),
        "message": ui.lbl_info.value or ("ON AIR" if ui.on_air else "OFF AIR"),
    }

def push_event(evt: dict):
    """Guardar y broadcast a todos los WS conectados."""
    evt["ts"] = dt.datetime.now().isoformat()
    events_deque.append(evt)
    asyncio.create_task(fs_add_event(evt))
    # broadcast no-bloqueante
    if ws_clients:
        data = json.dumps(evt)
        dead = []
        for ws in list(ws_clients):
            try:
                asyncio.create_task(ws.send_str(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

# ---- Normalizador de URL para reproductor (Shoutcast/Icecast) ----
def _player_friendly_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    
    # Caso específico Lobo (puerto 30180): múltiples intentos
    if "37.157.242.105:30180" in u or "lobo" in u.lower():
        # Lista de URLs alternativas para Lobo Radio
        lobo_urls = [
            "http://37.157.242.105:30180/;",
            "http://37.157.242.105:30180/stream",
            "http://37.157.242.105:30180/",
            "http://37.157.242.105:30180"
        ]
        # Si ya tiene una forma específica, usarla
        if u.endswith(";") or "/stream" in u:
            return u
        # Por defecto, usar la primera
        return lobo_urls[0]
    
    # Genérico Shoutcast
    if ";stream" in u:
        base = u.split(";")[0]
        if not base.endswith("/"):
            base += "/"
        return base + ";"
    if u.endswith("/stream") or u.endswith("/stream.mp3"):
        base = u.rsplit("/", 1)[0]
        if not base.endswith("/"):
            base += "/"
        return base + ";"
    return u

# ---- Prefs por URL ----
PREF_KEYS = [
    "name", "from", "recipients",
    "silence_seconds", "db_threshold", "cooldown_seconds",
    "email_body_alert", "email_body_ok",
]

def _apply_saved_prefs(cfg: dict, st: dict):
    url_key = (st.get("url") or "").strip().lower()
    stored = cfg.get("stations", {}).get(url_key)
    if stored:
        for k in PREF_KEYS:
            if k in stored:
                st[k] = stored[k]

def _store_prefs(cfg: dict, st: dict):
    url_key = (st.get("url") or "").strip().lower()
    if not url_key:
        return
    cfg.setdefault("stations", {})
    cfg["stations"][url_key] = {k: st.get(k) for k in PREF_KEYS}
    save_cfg(cfg)

# ---------- Monitor de una estación ----------
class StationMonitor:
    def __init__(self, page: ft.Page, cfg: dict):
        self.page = page
        self.cfg = cfg
        self.proc = None
        self.stop_flag = False

        self.on_air = True
        self.alert_active = False
        self.last_alert_ts = None
        self.silence_start_ts = None
        self.got_first_samples = False
        self.last_audio_ts = dt.datetime.now()
        self.ff_no_audio_tries = 0

        # === NUEVAS VARIABLES DE VALIDACIÓN ===
        self.silence_samples_count = 0  # contador de muestras bajo umbral
        self.audio_samples_count = 0  # contador de muestras con audio
        self.last_valid_audio_ts = dt.datetime.now()  # timestamp del último audio válido
        self.ffmpeg_restart_count = 0  # contador de reinicios de ffmpeg
        self.last_level_state = None  # último estado del nivel (above/below)

        # === NUEVAS VARIABLES PARA FUNCIONALIDADES ADICIONALES ===
        self.network_ok = True  # estado de la red
        self.last_network_check = dt.datetime.now()
        self.level_history = deque(maxlen=AUTO_ADJUST_SAMPLES)  # historial de niveles para auto-ajuste
        self.auto_threshold_enabled = False  # auto-ajuste de umbral activado
        self.session_start_ts = dt.datetime.now()  # inicio de sesión
        self.total_downtime = 0.0  # tiempo total fuera del aire en esta sesión
        self.downtime_events = 0  # número de veces que cayó
        self.current_schedule_profile = "normal"  # perfil de horario actual
        
        # === SISTEMA DE VERIFICACIÓN ESTRICTA ===
        self.pending_alert = False  # hay una alerta pendiente de verificación
        self.alert_verification_start = None  # cuándo empezó el periodo de gracia
        self.last_verification_check = None  # última vez que se verificó
        self.confirmed_downtime_start = None  # inicio confirmado de caída (después de verificación)
        self.brief_recovery_start = None  # inicio de recuperación breve (para ignorar)
        
        # === RDS / NOW PLAYING ===
        self.current_song = "Cargando..."  # canción actual
        self.last_rds_update = dt.datetime.now()
        self.rds_url = self._get_rds_url()  # URL de RDS para esta estación
        self.song_is_suspicious = False  # si la canción actual tiene patrón sospechoso
        
        # === CONTROL DE VENTANAS ===
        self.history_window_open = False  # flag para prevenir ventanas duplicadas
        self.settings_window_open = False  # flag para prevenir ventanas duplicadas

        # Soft detector
        self.soft_silence_start: dt.datetime | None = None
        self.simulating = False

        # reproductor
        self.audio: FAudio | None = None
        self.audio_slider: ft.Slider | None = None
        self.audio_playing = False

        # backends extra
        self.audio_backend = "flet"   # "flet" | "vlc" | "ffplay"
        self.vlc_instance = None
        self.vlc_player = None
        self.ffplay_proc: subprocess.Popen | None = None

        # Tareas asíncronas críticas
        self.ffmpeg_task: asyncio.Task | None = None
        self.rds_task: asyncio.Task | None = None
        self.anim_task: asyncio.Task | None = None
        self.stats_task: asyncio.Task | None = None
        self.keepalive_task: asyncio.Task | None = None

        # onda (ancho igual / alto reducido por escala)
        self.W = 480  # Ancho de onda
        self.H = 150  # Altura de onda - ajustado para que quepa perfectamente

        self.phase = 0.0
        self.current_db = -80.0

        # UI
        self._build_ui()
        # tareas se inician en start() tras page.update()

    def start(self):
        self.anim_task = self.page.run_task(self._anim_loop)
        self.stats_task = self.page.run_task(self._update_stats_loop)
        self.rds_task = self.page.run_task(self._rds_update_loop)  # Nuevo loop para RDS
        self.ffmpeg_task = self.page.run_task(self._run_ffmpeg)
        self.keepalive_task = self.page.run_task(self._keepalive_loop)
    
    def _get_rds_url(self) -> str:
        """Obtiene la URL de RDS para esta estación basado en su nombre."""
        name_lower = self.cfg.get("name", "").lower()
        for key, url in RDS_URLS.items():
            if key in name_lower:
                return url
        return ""

    def _build_ui(self):
        # Tamaños MUCHO más grandes para llenar pantalla verticalmente
        dot_size = 24
        status_size = 48
        card_pad = 20
        col_spacing = 12
        
        # ONDA MÁS GRANDE - clave para llenar espacio vertical
        self.W = 480  # Ancho de onda
        self.H = 150  # ALTURA DE ONDA ajustada para que quepa perfectamente

        self.title = ft.Text(self.cfg["name"], size=22, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_200)
        self.dot = ft.Container(width=dot_size, height=dot_size, bgcolor=ft.Colors.GREEN_400, border_radius=50)
        self.lbl_status = ft.Text("ON AIR", size=status_size, weight=ft.FontWeight.W_800)
        status_row = ft.Row([self.dot, ft.Container(width=10), self.lbl_status],
                            alignment=ft.MainAxisAlignment.CENTER,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # --- paints: glow + linea principal ---
        # --- Wave: glow + main (para estilo moderno y color dinámico) ---
        self.paint_glow = ft.Paint(
            stroke_width=6,
            style=ft.PaintingStyle.STROKE,
            color=ft.Colors.with_opacity(0.14, ft.Colors.GREEN_900),
        )
        self.paint_main = ft.Paint(
            stroke_width=2.4,
            style=ft.PaintingStyle.STROKE,
            color=ft.Colors.with_opacity(0.85, ft.Colors.GREEN_700),
        )

        self.path_glow = cv.Path(
            elements=[cv.Path.MoveTo(0, self.H/2), cv.Path.LineTo(self.W, self.H/2)],
            paint=self.paint_glow
        )
        self.path = cv.Path(
            elements=[cv.Path.MoveTo(0, self.H/2), cv.Path.LineTo(self.W, self.H/2)],
            paint=self.paint_main
        )

        self.wave = ft.Container(
            content=cv.Canvas([self.path_glow, self.path]),
            bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.BLUE_GREY_100),
            border_radius=12, padding=0, width=self.W, height=self.H
        )

        self.lbl_db = ft.Text("Nivel: -∞ dBFS", color=ft.Colors.GREY_400, size=13)
        self.lbl_info = ft.Text("Conectando…", color=ft.Colors.GREY_500, size=14)
        self.lbl_timer = ft.Text("", color=ft.Colors.GREY_500, size=13)
        self.lbl_stats = ft.Text("", color=ft.Colors.GREY_600, size=12)
        self.lbl_schedule = ft.Text("", color=ft.Colors.BLUE_400, size=12)
        
        # === RDS / NOW PLAYING ===
        self.lbl_song = ft.Text(
            "♪ Cargando...",
            size=14,
            color=ft.Colors.CYAN_400,
            italic=True,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS
        )

        self.btn_settings = ft.IconButton(
            icon=ft.Icons.SETTINGS, 
            tooltip="Preferencias", 
            on_click=self.open_settings,
            icon_size=22
        )
        self.btn_history = ft.IconButton(
            icon=ft.Icons.HISTORY,
            tooltip="Historial de canciones",
            on_click=self.open_song_history,
            icon_size=22
        )
        self.btn_audio = ft.IconButton(
            icon=ft.Icons.VOLUME_UP, 
            tooltip="Escuchar", 
            on_click=self.toggle_audio,
            icon_size=22
        )
        self.btn_delete = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE, 
            tooltip="Eliminar estación",
            on_click=lambda e: self.remove_self(),
            icon_size=22
        )

        header = ft.Row(
            [self.title, ft.Container(expand=True), self.btn_history, self.btn_settings, self.btn_audio, self.btn_delete],
            vertical_alignment=ft.CrossAxisAlignment.CENTER
        )

        self.card = ft.Container(
            content=ft.Column(
                [header, status_row, self.wave, self.lbl_db, self.lbl_song, self.lbl_info, self.lbl_timer, self.lbl_stats, self.lbl_schedule],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, 
                spacing=col_spacing,
                expand=True  # Column se expande para llenar la card
            ),
            border_radius=12, 
            padding=card_pad, 
            bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE),
            expand=True  # Card se expande para llenar su espacio
        )

    def remove_self(self):
        self.stop()
        try:
            app_state["stations_ui"].remove(self)
        except ValueError:
            pass
        col: ft.Column = app_state.get("stations_col")
        if col and self.card in col.controls:
            col.controls.remove(self.card)
        self.page.update()

    def ui_set_status(self, on_air: bool):
        self.on_air = on_air
        self.dot.bgcolor = ft.Colors.GREEN_400 if on_air else ft.Colors.RED_400
        self.lbl_status.value = "ON AIR" if on_air else "OFF AIR"

    def ui_set_level(self, dbfs: float):
        # Durante simulación, "congelamos" en -∞
        if self.simulating:
            self.current_db = -99.0
            self.lbl_db.value = "Nivel: -∞ dBFS"
            return

        self.current_db = float(dbfs)
        self.lbl_db.value = "Nivel: -∞ dBFS" if dbfs <= -99 else f"Nivel: {dbfs:0.1f} dBFS"
        
        # Marcar que recibimos audio válido
        self.audio_samples_count += 1
        
        # Agregar a historial para auto-ajuste
        if dbfs > -99:
            self.level_history.append(dbfs)
        
        if not self.got_first_samples:
            self.got_first_samples = True
            self.lbl_info.value = "Conectado"
            self.page.run_task(fs_write_station_if_changed, self)

        # === OBTENER PERFIL DE HORARIO ===
        schedule = get_current_schedule_profile()
        profile_name = "tolerant" if schedule == SCHEDULE_PROFILES["tolerant"] else "normal"
        if profile_name != self.current_schedule_profile:
            self.current_schedule_profile = profile_name
            logger.info(f"[{self.cfg['name']}] Cambio a perfil '{profile_name}': {schedule['description']}")

        # === AUTO-AJUSTE DE UMBRAL ===
        th = float(self.cfg["db_threshold"])
        if self.auto_threshold_enabled and len(self.level_history) >= AUTO_ADJUST_SAMPLES:
            avg_level = sum(self.level_history) / len(self.level_history)
            suggested_threshold = avg_level - AUTO_ADJUST_MARGIN
            # Solo ajustar si es significativamente diferente
            if abs(suggested_threshold - th) > 3.0:
                th = suggested_threshold
                self.cfg["db_threshold"] = th
                logger.info(f"[{self.cfg['name']}] Umbral auto-ajustado a {th:.1f} dB (promedio: {avg_level:.1f})")

        # Aplicar offset del perfil de horario
        th_adjusted = th + schedule["db_offset"]

        # ---- DETECTOR MEJORADO CON VALIDACIÓN ANTI-FALSAS ALERTAS ----
        now = dt.datetime.now()
        
        # Determinar estado actual con histéresis
        if dbfs <= th_adjusted:
            current_state = "below"
        elif dbfs >= th_adjusted + LEVEL_HYSTERESIS:
            current_state = "above"
        else:
            # Zona de histéresis: mantener estado previo
            current_state = self.last_level_state if self.last_level_state else "above"
        
        # Detección de silencio con confirmación
        if current_state == "below":
            self.silence_samples_count += 1
            
            # Requiere múltiples muestras consecutivas para confirmar silencio
            if self.silence_samples_count >= SILENCE_CONFIRMATION_SAMPLES:
                if self.soft_silence_start is None:
                    self.soft_silence_start = now
                else:
                    dur = (now - self.soft_silence_start).total_seconds()
                    # Aplicar multiplicador de tiempo del perfil de horario
                    silence_threshold = float(self.cfg["silence_seconds"]) * schedule["silence_multiplier"]
                    
                    if (not self.alert_active) and dur >= silence_threshold:
                        logger.info(f"[{self.cfg['name']}] Silencio confirmado tras {self.silence_samples_count} muestras (dur={dur:.1f}s, umbral={silence_threshold:.1f}s)")
                        self._on_silence_start()
        else:
            # Audio detectado
            if current_state == "above":
                self.last_valid_audio_ts = now
                self.silence_samples_count = 0  # resetear contador de silencio
                
                # Recuperación con histéresis
                if self.soft_silence_start is not None:
                    self.soft_silence_start = None
                    if self.alert_active:
                        dur = (now - self.silence_start_ts).total_seconds() if self.silence_start_ts else 0.0
                        logger.info(f"[{self.cfg['name']}] Audio recuperado (nivel={dbfs:.1f} dB, dur={dur:.1f}s)")
                        self._on_silence_end(dur)
        
        self.last_level_state = current_state

    def open_song_history(self, _=None):
        """Muestra el historial completo de canciones con detección de patrones."""
        # Prevenir abrir múltiples ventanas
        if self.history_window_open:
            return
        
        # CRÍTICO: Limpiar todos los overlays primero
        try:
            self.page.overlay.clear()
            self.page.update()
        except Exception:
            pass
        
        # Verificar inicialización
        if not hasattr(self, 'card') or self.card is None:
            return
        
        # Marcar ventana como abierta
        self.history_window_open = True
        
        stats = get_song_stats(self.cfg["name"])
        all_songs = stats["top_songs"]  # Ya viene ordenado por repeticiones
        timestamps = stats["timestamps"]
        
        overlay = ft.Container(expand=True, bgcolor=ft.Colors.with_opacity(0.7, ft.Colors.BLACK))
        
        def close_dialog(_=None):
            self.history_window_open = False  # Liberar flag
            try:
                self.page.overlay.clear()
            except Exception:
                pass
            try:
                self.page.update()
            except Exception:
                pass
        
        def clear_history(_=None):
            """Borra todo el historial de RDS."""
            self.history_window_open = False  # Liberar flag
            try:
                if os.path.exists(RDS_HISTORY_FILE):
                    os.remove(RDS_HISTORY_FILE)
                logger.info(f"[{self.cfg['name']}] Historial RDS borrado")
                close_dialog()
                # Reabrir para mostrar vacío
                self.open_song_history()
            except Exception as e:
                logger.error(f"Error borrando historial: {e}")
        
        def ignore_alert_click(song_name: str):
            """Ignora la alerta de una canción específica."""
            self.history_window_open = False  # Liberar flag
            ignore_alert(song_name, self.cfg["name"])
            close_dialog()
            # Reabrir para actualizar
            self.open_song_history()
        
        def show_song_times(song_name: str):
            """Muestra todas las veces que sonó una canción con diseño mejorado."""
            # NO liberar flag aquí porque es ventana secundaria
            times = timestamps.get(song_name, [])
            
            # Formatear fechas bonitas
            formatted_times = []
            for t in times[-100:]:  # Últimas 100
                try:
                    ts = dt.datetime.fromisoformat(t)
                    formatted_times.append(f"📅 {ts.strftime('%Y-%m-%d')} - {ts.strftime('%H:%M:%S')}")
                except Exception:
                    formatted_times.append(f"📅 {t}")
            
            times_text = "\n".join(formatted_times) if formatted_times else "Sin registros"
            
            detail_overlay = ft.Container(expand=True, bgcolor=ft.Colors.with_opacity(0.8, ft.Colors.BLACK))
            
            def close_detail(_=None):
                try:
                    self.page.overlay.remove(detail_overlay)
                except Exception:
                    pass
                self.page.update()
            
            # Acortar título si es muy largo
            display_title = song_name if len(song_name) <= 40 else song_name[:37] + "..."
            
            detail_panel = ft.Container(
                width=700,
                height=600,
                padding=20,
                bgcolor=ft.Colors.BLUE_GREY_800,
                border_radius=16,
                content=ft.Column(
                    [
                        ft.Row([
                            ft.Text(
                                f"Historial: {display_title}",
                                size=16,
                                weight=ft.FontWeight.W_600,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS
                            ),
                            ft.Container(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                on_click=close_detail,
                                tooltip="Cerrar",
                                icon_size=20
                            )
                        ]),
                        ft.Row([
                            ft.FilledButton(
                                "Agregar a exclusiones",
                                icon=ft.Icons.BLOCK,
                                on_click=lambda e: add_exclusion(song_name)
                            )
                        ]),
                        ft.Divider(),
                        ft.Container(
                            content=ft.Text(
                                times_text,
                                size=12,
                                selectable=True
                            ),
                            expand=True,
                            padding=10,
                            bgcolor=ft.Colors.with_opacity(0.3, ft.Colors.BLACK),
                            border_radius=8
                        )
                    ],
                    spacing=10
                )
            )
            
            detail_scrim = ft.Container(expand=True, on_click=close_detail)
            detail_overlay.content = ft.Stack(
                controls=[
                    detail_scrim,
                    ft.Container(expand=True, alignment=ft.alignment.center, content=detail_panel)
                ]
            )
            
            self.page.overlay.append(detail_overlay)
            self.page.update()
        
        def add_exclusion(song_name: str):
            """Agrega una canción a la lista de exclusiones."""
            exclusions_file = "rds_exclusions.json"
            try:
                if os.path.exists(exclusions_file):
                    with open(exclusions_file, "r", encoding="utf-8") as f:
                        exclusions = json.load(f)
                else:
                    exclusions = []
                
                if song_name not in exclusions:
                    exclusions.append(song_name)
                    with open(exclusions_file, "w", encoding="utf-8") as f:
                        json.dump(exclusions, f, indent=2, ensure_ascii=False)
                    
                    logger.info(f"[{self.cfg['name']}] Exclusión agregada: {song_name}")
                    close_dialog()
                    # Reabrir historial actualizado
                    self.open_song_history()
            except Exception as e:
                logger.error(f"Error agregando exclusión: {e}")
        
        # Cargar exclusiones
        exclusions_file = "rds_exclusions.json"
        exclusions = []
        if os.path.exists(exclusions_file):
            try:
                with open(exclusions_file, "r", encoding="utf-8") as f:
                    exclusions = json.load(f)
            except Exception:
                pass
        
        # Crear lista de canciones
        # Separar: con alerta (arriba) y sin alerta (medio) y alertas ignoradas (abajo)
        songs_with_alert = []
        songs_normal = []
        songs_ignored_alert = []
        
        for song, count in all_songs:  # Ya viene ordenado por repeticiones
            # Saltar si está en exclusiones
            if song in exclusions:
                continue
            
            # Detectar patrón y si está ignorado
            is_suspicious = check_suspicious_pattern(song, self.cfg["name"])
            is_ignored = is_alert_ignored(song, self.cfg["name"])
            
            # Acortar nombre si es muy largo
            display_song = song if len(song) <= 50 else song[:47] + "..."
            
            row = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            content=ft.Text(
                                f"🚨 {display_song}" if (is_suspicious and not is_ignored) else display_song,
                                size=12,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                weight=ft.FontWeight.W_500,
                                color=ft.Colors.RED_400 if (is_suspicious and not is_ignored) else ft.Colors.WHITE
                            ),
                            expand=True
                        ),
                        ft.Text(f"{count}x", size=11, color=ft.Colors.GREY_400, width=50),
                        ft.OutlinedButton(
                            "Ignorar",
                            on_click=lambda e, s=song: ignore_alert_click(s),
                            height=30,
                            width=90
                        ) if (is_suspicious and not is_ignored) else ft.Container(width=90)
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER
                ),
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                on_click=lambda e, s=song: show_song_times(s),
                bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.WHITE),
                border_radius=8,
                ink=True
            )
            
            if is_suspicious and not is_ignored:
                songs_with_alert.append(row)
            elif is_ignored:
                songs_ignored_alert.append(row)
            else:
                songs_normal.append(row)
        
        # Combinar: alertas arriba, normales medio, ignoradas abajo
        song_rows = songs_with_alert + songs_normal + songs_ignored_alert
        
        if not song_rows:
            song_rows.append(
                ft.Text("No hay canciones registradas aún", size=12, color=ft.Colors.GREY_500)
            )
        
        # Acortar título si es muy largo
        station_title = self.cfg['name'] if len(self.cfg['name']) <= 30 else self.cfg['name'][:27] + "..."
        
        panel = ft.Container(
            width=800,
            height=700,
            padding=20,
            bgcolor=ft.Colors.BLUE_GREY_900,
            border_radius=16,
            content=ft.Column(
                [
                    ft.Row([
                        ft.Text(
                            f"🎵 Historial - {station_title}",
                            size=18,
                            weight=ft.FontWeight.W_700,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True
                        ),
                        ft.OutlinedButton(
                            "Borrar historial",
                            icon=ft.Icons.DELETE_FOREVER,
                            on_click=clear_history
                        ),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE,
                            on_click=close_dialog,
                            tooltip="Cerrar"
                        )
                    ]),
                    ft.Text(
                        f"Total: {stats['total_tracked']} | Exclusiones: {len(exclusions)} | 🚨 = Patrón sospechoso",
                        size=11,
                        color=ft.Colors.GREY_400
                    ),
                    ft.Divider(),
                    ft.Container(
                        content=ft.Column(
                            song_rows,
                            spacing=4,
                            scroll=ft.ScrollMode.AUTO
                        ),
                        expand=True
                    )
                ],
                spacing=10
            )
        )
        
        scrim = ft.Container(expand=True, on_click=close_dialog)
        overlay.content = ft.Stack(
            controls=[
                scrim,
                ft.Container(expand=True, alignment=ft.alignment.center, content=panel)
            ]
        )
        
        self.page.overlay.append(overlay)
        self.page.update()

    # ---- Panel lateral (overlay) ----
    def open_settings(self, _=None):
        """Panel de configuración."""
        # Prevenir abrir múltiples ventanas
        if self.settings_window_open:
            return
        
        # CRÍTICO: Limpiar todos los overlays primero
        try:
            self.page.overlay.clear()
            self.page.update()
        except Exception:
            pass
        
        # Marcar ventana como abierta
        self.settings_window_open = True
        
        st = self.cfg
        ent_name = ft.TextField(label="Nombre", value=st["name"])
        ent_url  = ft.TextField(label="URL", value=st["url"])
        ent_from = ft.TextField(label="Remitente (From)", value=st.get("from", DEFAULT_FROM))

        # cálculo para alinear 3 cajas al ancho útil del panel
        panel_width = 380
        pad = 16
        spacing = 12
        usable = panel_width - 2*pad
        triple_w = int((usable - 2*spacing) // 3)  # = 108 con estos números

        ent_sil  = ft.TextField(label="Seg. silencio", value=str(st["silence_seconds"]), width=triple_w)
        ent_db   = ft.TextField(label="Umbral dB", value=str(abs(st["db_threshold"])), width=triple_w)
        ent_cd   = ft.TextField(label="Cooldown", value=str(st["cooldown_seconds"]), width=triple_w)
        tb_rcpt  = ft.TextField(label="Destinatarios (uno por línea)",
                                multiline=True, min_lines=4, max_lines=6, value="\n".join(st.get("recipients", [])))
        tb_alert = ft.TextField(label="Texto ALERTA", multiline=True, min_lines=2, value=st["email_body_alert"])
        tb_ok    = ft.TextField(label="Texto RECUPERADO", multiline=True, min_lines=2, value=st["email_body_ok"])

        overlay = ft.Container(expand=True, bgcolor=ft.Colors.with_opacity(0.7, ft.Colors.BLACK))

        def close_overlay(_=None):
            self.settings_window_open = False  # Liberar flag
            try:
                self.page.overlay.clear()
            except Exception:
                pass
            try:
                self.page.update()
            except Exception:
                pass

        def save(_=None):
            old_url = st["url"].strip()
            st["name"] = ent_name.value.strip() or st["name"]
            st["url"]  = ent_url.value.strip() or st["url"]
            st["from"] = ent_from.value.strip() or st.get("from", DEFAULT_FROM)
            try:
                st["silence_seconds"] = max(1, int(float(ent_sil.value.strip())))
            except: pass
            try:
                val = float(ent_db.value.strip())
                st["db_threshold"] = -abs(val)
            except: pass
            try:
                st["cooldown_seconds"] = max(1, int(float(ent_cd.value.strip())))
            except: pass
            st["recipients"] = [r.strip() for r in tb_rcpt.value.strip().splitlines() if r.strip()]
            st["email_body_alert"] = tb_alert.value.strip() or st["email_body_alert"]
            st["email_body_ok"]    = tb_ok.value.strip() or st["email_body_ok"]
            self.title.value = st["name"]

            cfg = app_state["cfg"]
            if st["url"].strip().lower() != old_url.lower():
                cfg.get("stations", {}).pop(old_url.lower(), None)
            _store_prefs(cfg, st)

            # Liberar flag ANTES de cerrar
            self.settings_window_open = False
            
            # Cerrar overlay sin update (para evitar socket error)
            try:
                self.page.overlay.clear()
            except Exception:
                pass

        def test_smtp(_=None):
            ok = send_mail(st, f"🔧 Prueba SMTP — {st['name']}", "Prueba SMTP del monitor.")
            self.lbl_info.value = "SMTP OK" if ok else "SMTP falló (ver log)"
            close_overlay()

        def simulate_click(_):
            # ejecuta la corrutina pasando 60 segundos
            self.page.run_task(self.simulate_silence_for, 60)

        def restart(_=None):
            self.lbl_info.value = "Reiniciando captura…"
            self.page.update()
            self._kill_ffmpeg()
            close_overlay()

        def toggle_auto_adjust(_=None):
            self.auto_threshold_enabled = not self.auto_threshold_enabled
            status = "activado" if self.auto_threshold_enabled else "desactivado"
            self.lbl_info.value = f"Auto-ajuste {status}"
            logger.info(f"[{self.cfg['name']}] Auto-ajuste de umbral {status}")
            close_overlay()

        def show_stats(_=None):
            stats = get_station_stats(st["url"], hours=24)
            stats_text = (
                f"📊 Estadísticas (últimas 24h)\n\n"
                f"Uptime: {stats['uptime_percent']:.1f}%\n"
                f"Caídas: {stats['downtime_count']}\n"
                f"Tiempo fuera: {human_duration(stats['total_downtime_seconds'])}\n"
                f"Eventos registrados: {stats['events_count']}"
            )
            self.lbl_info.value = stats_text.replace('\n', ' | ')
            self.page.update()
            import time
            time.sleep(5)
            self.lbl_info.value = "Conectado" if self.on_air else "Silencio detectado…"
            self.page.update()
        
        panel = ft.Container(
            width=panel_width, padding=pad, bgcolor=ft.Colors.BLUE_GREY_900, border_radius=12,
            content=ft.Column(
                [
                    ft.Row([ft.Text("Preferencias — "+st["name"], size=16, weight=ft.FontWeight.W_600),
                            ft.Container(expand=True),
                            ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Cerrar", on_click=close_overlay)],
                           alignment=ft.MainAxisAlignment.START),
                    ent_name, ent_url, ent_from,
                    ft.Row([ent_sil, ent_db, ent_cd], spacing=spacing),
                    tb_rcpt, tb_alert, tb_ok,
                    ft.Row([ft.FilledButton("Guardar", on_click=save),
                            ft.OutlinedButton("Probar SMTP", on_click=test_smtp)],
                           alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                    ft.Row([ft.ElevatedButton("Simular silencio", on_click=simulate_click),
                            ft.ElevatedButton("Reiniciar", on_click=restart)],
                           alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                    ft.Row([ft.TextButton("Auto-ajuste umbral", on_click=toggle_auto_adjust),
                            ft.TextButton("Ver estadísticas", on_click=show_stats)],
                           alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8, scroll=ft.ScrollMode.AUTO, tight=True
            )
        )
        scrim = ft.Container(expand=True, bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK), on_click=close_overlay)
        overlay.content = ft.Stack(controls=[scrim, ft.Container(expand=True, alignment=ft.alignment.center, content=panel)])
        self.page.overlay.append(overlay)
        self.page.update()

    # -------- Backends de audio ----------
    def _choose_backend_for_url(self, url: str) -> str:
        # Para Lobo, preferencia: VLC > ffplay > flet (VLC maneja mejor Shoutcast)
        if "37.157.242.105:30180" in (url or "") or "lobo" in (url or "").lower():
            if HAS_VLC:
                logger.info(f"[{self.cfg['name']}] Usando VLC para Lobo Radio")
                return "vlc"
            try:
                _ = subprocess.run([ffplay_path(), "-version"], capture_output=True, timeout=5)
                logger.info(f"[{self.cfg['name']}] Usando ffplay para Lobo Radio")
                return "ffplay"
            except Exception:
                logger.info(f"[{self.cfg['name']}] Usando flet para Lobo Radio (menos confiable)")
                return "flet" if HAS_FLET_AUDIO else "none"
        
        # Para otras estaciones, flet es suficiente
        return "flet" if HAS_FLET_AUDIO else ("vlc" if HAS_VLC else "ffplay")

    def _start_audio_vlc(self, url: str):
        try:
            if not HAS_VLC:
                return False
            
            # Opciones específicas para mejorar compatibilidad con Shoutcast
            vlc_args = [
                "--no-video",
                "--quiet",
                "--intf", "dummy",
                "--http-reconnect",  # reconectar automáticamente
                "--network-caching=3000",  # 3 segundos de cache
            ]
            
            # Opciones extra para Lobo Radio
            if "37.157.242.105:30180" in url or "lobo" in url.lower():
                vlc_args.extend([
                    "--http-continuous",  # mantener conexión continua
                    "--demux=mp3",  # forzar demuxer MP3
                ])
                logger.info(f"[{self.cfg['name']}] Iniciando VLC para Lobo con opciones especiales")
            
            self.vlc_instance = vlc.Instance(*vlc_args)
            self.vlc_player = self.vlc_instance.media_player_new()
            m = self.vlc_instance.media_new(url)
            
            # User agent personalizado
            m.add_option(":http-user-agent=VLC/3.0 (Monitor On-Air)")
            m.add_option(":icy-metadata=1")
            
            self.vlc_player.set_media(m)
            self.vlc_player.audio_set_volume(50)
            self.vlc_player.play()
            
            logger.info(f"[{self.cfg['name']}] VLC iniciado correctamente")
            return True
        except Exception as e:
            logger.error(f"VLC play error: {e}")
            self.vlc_instance = None
            self.vlc_player = None
            return False

    def _start_audio_ffplay(self, url: str):
        try:
            cmd = [
                ffplay_path(), 
                "-nodisp", 
                "-autoexit",
                "-loglevel", "warning",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ]
            
            # Opciones específicas para Lobo
            if "37.157.242.105:30180" in url or "lobo" in url.lower():
                cmd.extend([
                    "-timeout", "10000000",  # 10 segundos timeout
                    "-fflags", "+discardcorrupt",  # descartar paquetes corruptos
                ])
                logger.info(f"[{self.cfg['name']}] Iniciando ffplay para Lobo con opciones especiales")
            
            cmd.extend(["-i", url])
            
            self.ffplay_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CF,
                startupinfo=_SI,
            )
            if os.name == "nt":
                try: _hide_console_windows_for_pid(self.ffplay_proc.pid)
                except Exception: pass
            
            logger.info(f"[{self.cfg['name']}] ffplay iniciado correctamente")
            return True
        except Exception as e:
            logger.error(f"ffplay error: {e}")
            self.ffplay_proc = None
            return False

    # ---- Reproductor ----
    def toggle_audio(self, _=None):
        if self.audio_playing:
            self._stop_audio()
            return

        # parar otras estaciones
        for s in app_state["stations_ui"]:
            if s is not self:
                s._stop_audio()

        play_src = _player_friendly_url(self.cfg["url"])
        backend = self._choose_backend_for_url(play_src)
        self.audio_backend = backend

        ok = False
        if backend == "vlc":
            ok = self._start_audio_vlc(play_src)
        elif backend == "ffplay":
            ok = self._start_audio_ffplay(play_src)
        else:
            if HAS_FLET_AUDIO:
                try:
                    self.audio = FAudio(src=play_src, autoplay=True, volume=0.5)
                    self.page.overlay.append(self.audio)
                    ok = True
                except Exception as e:
                    logger.error(f"flet-audio error: {e}")
                    self.audio = None
                    ok = False
            else:
                logger.warning("flet-audio no disponible")
                ok = False

        # Fallback si falla el backend elegido
        if not ok and backend != "flet" and HAS_FLET_AUDIO:
            try:
                self.audio = FAudio(src=play_src, autoplay=True, volume=0.5)
                self.page.overlay.append(self.audio)
                self.audio_backend = "flet"
                ok = True
            except Exception as e:
                logger.error(f"fallback flet-audio error: {e}")
                self.audio_backend = "flet"
                self.audio = None
                ok = False
        elif not ok and not HAS_FLET_AUDIO and HAS_VLC:
            # Si no hay flet-audio, intentar VLC como fallback
            ok = self._start_audio_vlc(play_src)
            if ok:
                self.audio_backend = "vlc"
        elif not ok and not HAS_FLET_AUDIO and not HAS_VLC:
            # Último recurso: ffplay
            ok = self._start_audio_ffplay(play_src)
            if ok:
                self.audio_backend = "ffplay"

        if not ok:
            snack(self.page, "No se pudo reproducir el stream.")
            return

        self.audio_playing = True

        header: ft.Row = self.card.content.controls[0]
        if not self.audio_slider:
            self.audio_slider = ft.Slider(min=0, max=100, value=50, width=120)

        if self.audio_slider not in header.controls:
            header.controls.append(self.audio_slider)

        if self.audio_backend == "vlc":
            self.audio_slider.on_change = lambda e: self.vlc_player and self.vlc_player.audio_set_volume(int(e.control.value))
            self.audio_slider.disabled = False
        elif self.audio_backend == "flet":
            self.audio_slider.on_change = lambda e: self._set_volume(e.control.value)
            self.audio_slider.disabled = False
        else:
            # ffplay sin control runtime
            self.audio_slider.on_change = None
            self.audio_slider.disabled = True

        self.page.update()

    def _set_volume(self, v):
        if self.audio_backend == "flet" and self.audio:
            self.audio.volume = max(0.0, min(1.0, float(v)/100.0))
            self.audio.update()
        elif self.audio_backend == "vlc" and self.vlc_player:
            try:
                self.vlc_player.audio_set_volume(int(max(0, min(100, float(v)))))
            except Exception:
                pass

    def _stop_audio(self):
        # flet-audio
        if self.audio:
            try:
                self.page.overlay.remove(self.audio)
            except Exception:
                pass
            self.audio = None

        # VLC
        if self.vlc_player:
            try:
                self.vlc_player.stop()
            except Exception:
                pass
            self.vlc_player = None
        if self.vlc_instance:
            try:
                self.vlc_instance.release()
            except Exception:
                pass
            self.vlc_instance = None

        # ffplay
        if self.ffplay_proc and self.ffplay_proc.poll() is None:
            try:
                self.ffplay_proc.terminate()
            except Exception:
                pass
        self.ffplay_proc = None

        self.audio_playing = False
        header: ft.Row = self.card.content.controls[0]
        if self.audio_slider and self.audio_slider in header.controls:
            self.audio_slider.disabled = False
            header.controls.remove(self.audio_slider)
        self.page.update()

    # ---- Simular silencio ----
    async def simulate_silence_for(self, seconds: int = 60):
        if self.simulating:
            return
        self.simulating = True
        self.current_db = -99.0
        self._on_silence_start()
        await asyncio.sleep(max(1, int(seconds)))
        self.simulating = False
        self._on_silence_end(float(seconds))

    # ---- FFmpeg MEJORADO CON MANEJO ROBUSTO ----
    async def _run_ffmpeg(self):
        backoff = 1
        while not self.stop_flag:
            try:
                n = self.cfg["db_threshold"]
                if n > 0: n = -abs(n)
                filt = f"silencedetect=n={n}dB:d={self.cfg['silence_seconds']}"
                
                # Timeout de conexión más corto
                cmd = [
                    ffmpeg_path(), "-hide_banner",
                    "-timeout", "15000000",  # 15 segundos en microsegundos
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-vn", "-i", self.cfg["url"],
                    "-af", filt,
                    "-ac", "1", "-ar", "16000",
                    "-f", "s16le", "pipe:1"
                ]
                
                self.lbl_info.value = "Conectando…"
                self.page.update()
                
                # Intentar crear proceso con timeout
                try:
                    self.proc = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            *cmd,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            creationflags=_CF,
                            startupinfo=_SI,
                        ),
                        timeout=20.0  # timeout de 20 segundos para crear el proceso
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[{self.cfg['name']}] Timeout al crear proceso FFmpeg")
                    self.ffmpeg_restart_count += 1
                    
                    # Si falla al inicio varias veces, marcar OFF
                    if self.ffmpeg_restart_count >= OFF_AFTER_RETRIES and not self.alert_active:
                        self.lbl_info.value = f"Falla de conexión ({self.ffmpeg_restart_count} intentos)"
                        self._on_silence_start()
                        self.page.update()
                    
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                
                if os.name == "nt":
                    try: _hide_console_windows_for_pid(self.proc.pid)
                    except Exception: pass

                start_ts = dt.datetime.now()
                had_audio = False

                async def read_stdout():
                    nonlocal had_audio
                    while True:
                        try:
                            chunk = await asyncio.wait_for(self.proc.stdout.read(4096), timeout=10.0)
                            if not chunk:
                                break
                            had_audio = True
                            self.last_audio_ts = dt.datetime.now()
                            a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                            if a.size == 0:
                                continue
                            rms = float(np.sqrt(np.mean(a*a))) / 32768.0
                            db = 20.0 * math.log10(max(rms, 1e-9))
                            self.ui_set_level(db)
                            # resetear contadores al recibir audio
                            self.ff_no_audio_tries = 0
                            self.ffmpeg_restart_count = 0
                        except asyncio.TimeoutError:
                            logger.warning(f"[{self.cfg['name']}] Timeout leyendo stdout")
                            break
                        except Exception as e:
                            logger.error(f"[{self.cfg['name']}] Error en read_stdout: {e}")
                            break

                async def read_stderr():
                    buf = b""
                    while True:
                        try:
                            chunk = await asyncio.wait_for(self.proc.stderr.read(4096), timeout=10.0)
                            if not chunk:
                                break
                            buf += chunk
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                s = line.decode("utf-8", "ignore").strip()
                                if "silence_start" in s:
                                    logger.info(f"[{self.cfg['name']}] FFmpeg detectó inicio de silencio")
                                    self._on_silence_start()
                                elif "silence_end" in s and "silence_duration" in s:
                                    try:
                                        dur = float(s.split("silence_duration:")[1].strip())
                                    except Exception:
                                        dur = 0.0
                                    logger.info(f"[{self.cfg['name']}] FFmpeg detectó fin de silencio (dur={dur:.1f}s)")
                                    self._on_silence_end(dur)
                        except asyncio.TimeoutError:
                            logger.warning(f"[{self.cfg['name']}] Timeout leyendo stderr")
                            break
                        except Exception as e:
                            logger.error(f"[{self.cfg['name']}] Error en read_stderr: {e}")
                            break
                    
                    # procesar remanente
                    if buf:
                        s = buf.decode("utf-8", "ignore").strip()
                        if "silence_start" in s:
                            logger.info(f"[{self.cfg['name']}] FFmpeg detectó inicio de silencio (remanente)")
                            self._on_silence_start()
                        elif "silence_end" in s and "silence_duration" in s:
                            try:
                                dur = float(s.split("silence_duration:")[1].strip())
                            except Exception:
                                dur = 0.0
                            logger.info(f"[{self.cfg['name']}] FFmpeg detectó fin de silencio (remanente, dur={dur:.1f}s)")
                            self._on_silence_end(dur)

                async def watchdog():
                    while True:
                        await asyncio.sleep(WATCHDOG_INTERVAL)
                        now = dt.datetime.now()

                        # === CHEQUEO DE RED ===
                        if (now - self.last_network_check).total_seconds() >= NETWORK_CHECK_INTERVAL:
                            self.last_network_check = now
                            network_ok = await check_network_connectivity()
                            if not network_ok and self.network_ok:
                                logger.warning(f"[{self.cfg['name']}] Red caída detectada")
                                self.lbl_info.value = "⚠️ Sin conexión a internet"
                                self.page.update()
                            elif network_ok and not self.network_ok:
                                logger.info(f"[{self.cfg['name']}] Red recuperada")
                                self.lbl_info.value = "Conectado"
                                self.page.update()
                            self.network_ok = network_ok

                        if had_audio:
                            gap = (now - self.last_audio_ts).total_seconds()
                            
                            # Solo alertar si gap supera umbral + tolerancia
                            if gap >= (float(self.cfg["silence_seconds"]) + AUDIO_GAP_TOLERANCE) and not self.alert_active:
                                # Verificar si es problema de red antes de alertar
                                if not self.network_ok:
                                    self.lbl_info.value = f"⚠️ Sin audio (red caída, {gap:.1f}s)"
                                else:
                                    self.lbl_info.value = f"Sin muestras ({gap:.1f}s)"
                                    self._on_silence_start()
                                self.page.update()

                            # Reiniciar si gap muy grande
                            if gap > float(self.cfg["silence_seconds"]) * 5:
                                logger.warning(f"[{self.cfg['name']}] Gap muy grande ({gap:.1f}s); reiniciando FFmpeg")
                                try:
                                    self.proc.kill()
                                except Exception:
                                    pass
                                break
                        else:
                            # Nunca recibimos audio
                            elapsed = (now - start_ts).total_seconds()
                            if elapsed >= (float(self.cfg["silence_seconds"]) + AUDIO_GAP_TOLERANCE) and not self.alert_active:
                                # Verificar si es problema de red
                                if not self.network_ok:
                                    self.lbl_info.value = f"⚠️ Sin conexión a internet ({elapsed:.1f}s)"
                                else:
                                    self.lbl_info.value = f"Sin audio inicial ({elapsed:.1f}s)"
                                    self._on_silence_start()
                                self.page.update()

                            if elapsed > float(self.cfg["silence_seconds"]) * 5:
                                logger.warning(f"[{self.cfg['name']}] Sin audio prolongado ({elapsed:.1f}s); reiniciando FFmpeg")
                                try:
                                    self.proc.kill()
                                except Exception:
                                    pass
                                break

                        # Reinicio preventivo
                        if (now - start_ts).total_seconds() > MAX_FFMPEG_LIFETIME:
                            logger.info(f"[{self.cfg['name']}] Reinicio preventivo ({MAX_FFMPEG_LIFETIME/3600:.1f}h)")
                            try:
                                self.proc.kill()
                            except Exception:
                                pass
                            break

                t_out = asyncio.create_task(read_stdout())
                t_err = asyncio.create_task(read_stderr())
                t_wdg = asyncio.create_task(watchdog())
                
                # Esperar con timeout
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=MAX_FFMPEG_LIFETIME + 10)
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.cfg['name']}] Timeout esperando proceso")
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                
                for t in (t_out, t_err, t_wdg): 
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

                rc = self.proc.returncode
                self.proc = None
                
                if self.stop_flag:
                    break

                if rc not in (0, None):
                    now = dt.datetime.now()

                    if not had_audio:
                        self.ff_no_audio_tries += 1
                        self.ffmpeg_restart_count += 1
                    else:
                        self.ff_no_audio_tries = 0
                        self.ffmpeg_restart_count = 0

                    # Marcar OFF después de varios intentos sin audio
                    if (self.ff_no_audio_tries >= OFF_AFTER_RETRIES) and not self.alert_active:
                        self.lbl_info.value = f"Sin audio tras {self.ff_no_audio_tries} intentos"
                        self._on_silence_start()
                        self.page.update()

                    # Verificar gap acumulado
                    gap = (now - self.last_audio_ts).total_seconds()
                    if (gap >= float(self.cfg["silence_seconds"]) + AUDIO_GAP_TOLERANCE) and not self.alert_active:
                        self._on_silence_start()

                    self.lbl_info.value = f"FFmpeg terminó (código {rc}); reintentando…"
                    self.page.update()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                
                # Reinicio exitoso, resetear backoff
                backoff = 1

            except Exception as e:
                logger.error(f"[{self.cfg['name']}] Excepción en _run_ffmpeg: {e}")
                self.ffmpeg_restart_count += 1
            
            await asyncio.sleep(backoff)
            backoff = min(backoff*2, 60)

    async def _keepalive_loop(self):
        """Vigila y revive tareas largas para sesiones abiertas por días."""
        while not self.stop_flag:
            await asyncio.sleep(60)

            # Si el loop de FFmpeg murió por excepción, reiniciarlo
            if self.ffmpeg_task and self.ffmpeg_task.done() and not self.stop_flag:
                exc = None
                try:
                    exc = self.ffmpeg_task.exception()
                except Exception:
                    pass
                if exc:
                    logger.error(f"[{self.cfg['name']}] Loop de captura terminó por excepción: {exc}")
                else:
                    logger.warning(f"[{self.cfg['name']}] Loop de captura terminado; reiniciando para mantener monitoreo")

                self._kill_ffmpeg()
                self.ffmpeg_task = self.page.run_task(self._run_ffmpeg)
                self.lbl_info.value = "Reconectando…"
                try:
                    self.page.update()
                except Exception:
                    pass

            # Reiniciar loop de RDS si se detuvo
            if self.rds_task and self.rds_task.done() and not self.stop_flag:
                self.rds_task = self.page.run_task(self._rds_update_loop)
                logger.info(f"[{self.cfg['name']}] Loop de RDS reiniciado tras detenerse")

    def _kill_ffmpeg(self):
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
        except Exception:
            pass

    # ---- Eventos de silencio CON VERIFICACIÓN ESTRICTA ----
    def _on_silence_start(self):
        """Detecta silencio pero NO envía email inmediatamente. Inicia periodo de gracia."""
        now = dt.datetime.now()
        
        if self.silence_start_ts is None:
            self.silence_start_ts = now
        
        self.ui_set_status(False)
        # NO incrementar downtime_events aquí - solo cuando envía email
        
        # Si ya hay una alerta confirmada activa, no hacer nada más
        if self.alert_active:
            return
        
        # Iniciar periodo de VERIFICACIÓN (no enviar email aún)
        if not self.pending_alert:
            self.pending_alert = True
            self.alert_verification_start = now
            self.last_verification_check = now
            
            elapsed = (now - self.silence_start_ts).total_seconds()
            
            # Indicar que está en periodo de verificación
            if not self.network_ok:
                self.lbl_info.value = f"⚠️ Verificando... Red caída ({elapsed:.0f}s)"
            else:
                self.lbl_info.value = f"🔍 Verificando silencio... ({elapsed:.0f}s / {GRACE_PERIOD_SECONDS}s)"
            
            logger.info(f"[{self.cfg['name']}] Silencio detectado - Iniciando periodo de verificación ({GRACE_PERIOD_SECONDS}s)")
            
            # Iniciar tarea de verificación
            self.page.run_task(self._verification_loop)
    
    async def _verification_loop(self):
        """Loop que verifica el silencio durante el periodo de gracia antes de enviar email."""
        consecutive_silent_checks = 0
        total_checks = 0
        audio_detected_count = 0
        
        logger.info(f"[{self.cfg['name']}] Iniciando loop de verificación - Duración: {GRACE_PERIOD_SECONDS}s")
        
        while self.pending_alert and not self.stop_flag:
            await asyncio.sleep(VERIFICATION_INTERVAL)
            
            now = dt.datetime.now()
            total_checks += 1
            
            # Verificar si REALMENTE hay silencio ahora
            current_level = self.current_db
            th = float(self.cfg["db_threshold"])
            schedule = get_current_schedule_profile()
            th_adjusted = th + schedule["db_offset"]
            
            is_silent_now = current_level <= th_adjusted
            
            if is_silent_now:
                consecutive_silent_checks += 1
                logger.debug(f"[{self.cfg['name']}] Check {total_checks}: SILENCIO (nivel: {current_level:.1f} dB <= {th_adjusted:.1f} dB)")
            else:
                audio_detected_count += 1
                logger.warning(f"[{self.cfg['name']}] Check {total_checks}: AUDIO detectado (nivel: {current_level:.1f} dB > {th_adjusted:.1f} dB)")
                
                # Si detecta audio en más del 30% de los checks, probablemente es falsa alarma
                audio_ratio = audio_detected_count / total_checks
                if audio_ratio > 0.3 and total_checks >= 3:
                    elapsed = (now - self.silence_start_ts).total_seconds() if self.silence_start_ts else 0
                    logger.info(f"[{self.cfg['name']}] CANCELANDO alerta - Audio detectado en {audio_ratio*100:.0f}% de checks tras {elapsed:.1f}s")
                    self.pending_alert = False
                    self.alert_verification_start = None
                    self.silence_start_ts = None
                    self.ui_set_status(True)
                    self.lbl_info.value = "Conectado - Falsa alarma evitada"
                    self.page.update()
                    return
            
            # Si recuperó audio (por otro método), cancelar alerta pendiente
            if self.on_air:
                elapsed = (now - self.silence_start_ts).total_seconds() if self.silence_start_ts else 0
                logger.info(f"[{self.cfg['name']}] Alerta cancelada - Audio recuperado (on_air=True) tras {elapsed:.1f}s")
                self.pending_alert = False
                self.alert_verification_start = None
                self.silence_start_ts = None
                self.lbl_info.value = "Conectado"
                self.page.update()
                return
            
            # Calcular tiempo transcurrido
            elapsed = (now - self.silence_start_ts).total_seconds() if self.silence_start_ts else 0
            grace_elapsed = (now - self.alert_verification_start).total_seconds()
            
            # Actualizar UI durante verificación
            remaining = max(0, GRACE_PERIOD_SECONDS - grace_elapsed)
            confidence = (consecutive_silent_checks / total_checks * 100) if total_checks > 0 else 0
            
            if not self.network_ok:
                self.lbl_info.value = f"⚠️ Verificando... Red caída ({elapsed:.0f}s)"
            else:
                self.lbl_info.value = f"🔍 Verificando... {remaining:.0f}s | Silencio: {confidence:.0f}% ({consecutive_silent_checks}/{total_checks}) | Nivel: {current_level:.1f}dB"
            self.page.update()
            
            # Si pasó el periodo de gracia Y sigue caído, CONFIRMAR y enviar email
            if grace_elapsed >= GRACE_PERIOD_SECONDS:
                # Verificar que al menos el 70% de los checks fueron silencio real (bajado de 80%)
                silence_confidence = (consecutive_silent_checks / total_checks * 100) if total_checks > 0 else 0
                
                logger.info(f"[{self.cfg['name']}] Fin de verificación - Silencio: {consecutive_silent_checks}/{total_checks} ({silence_confidence:.0f}%)")
                
                if silence_confidence >= 70:
                    logger.info(f"[{self.cfg['name']}] ✓✓✓ CAÍDA CONFIRMADA ✓✓✓ tras {elapsed:.1f}s ({silence_confidence:.0f}% confianza)")
                    self.pending_alert = False
                    self.confirmed_downtime_start = self.silence_start_ts
                    self._send_confirmed_alert(elapsed)
                else:
                    logger.warning(f"[{self.cfg['name']}] Alerta NO confirmada - Confianza insuficiente: {silence_confidence:.0f}% (requiere 70%)")
                    self.pending_alert = False
                    self.alert_verification_start = None
                    self.silence_start_ts = None
                    self.ui_set_status(True)
                    self.lbl_info.value = f"Conectado - Verificación fallida ({silence_confidence:.0f}% confianza)"
                    self.page.update()
                return
    
    def _send_confirmed_alert(self, total_downtime: float):
        """Envía el email SOLO después de confirmar que la caída es real."""
        now = dt.datetime.now()
        
        # Actualizar UI
        if not self.network_ok:
            self.lbl_info.value = f"🚨 CONFIRMADO: Red caída ({human_duration(total_downtime)})"
        else:
            self.lbl_info.value = f"🚨 CONFIRMADO: Fuera del aire ({human_duration(total_downtime)})"
        self.page.update()
        
        # === GUARDAR EN HISTORIAL ===
        event = {
            "timestamp": dt.datetime.now().isoformat(),
            "station_id": self.cfg.get("url", "").lower(),
            "station_name": self.cfg.get("name", ""),
            "event_type": "offline_confirmed",
            "network_issue": not self.network_ok,
            "level_dbfs": float(self.current_db),
            "verification_time": GRACE_PERIOD_SECONDS,
            "total_downtime": total_downtime
        }
        save_event_to_history(event)
        
        # Marcar alerta como activa ANTES de verificar cooldown
        self.alert_active = True
        
        # Verificar cooldown SOLO para email (no para marcar activa)
        cooldown = self.cfg["cooldown_seconds"]
        skip_email = False
        if self.last_alert_ts:
            last_alert_age = (now - self.last_alert_ts).total_seconds()
            if last_alert_age < cooldown:
                logger.info(f"[{self.cfg['name']}] Email omitido por cooldown ({last_alert_age:.0f}s < {cooldown}s)")
                skip_email = True
        
        if not skip_email:
            # Actualizar timestamp de última alerta
            self.last_alert_ts = now
            
            # INCREMENTAR contador de caídas SOLO cuando envía email
            self.downtime_events += 1
            
            # Personalizar mensaje según el tipo de problema
            if not self.network_ok:
                subj = f"🚨 {self.cfg['name']} - Problema de red CONFIRMADO"
                body = (
                    f"ALERTA VERIFICADA\n\n"
                    f"La estación '{self.cfg['name']}' NO es accesible.\n"
                    f"Problema detectado hace: {human_duration(total_downtime)}\n"
                    f"Tiempo de verificación: {human_duration(GRACE_PERIOD_SECONDS)}\n\n"
                    f"Posible problema de conectividad a internet.\n"
                    f"Inicio de caída: {self.silence_start_ts.strftime('%Y-%m-%d %H:%M:%S') if self.silence_start_ts else 'N/A'}\n"
                    f"Alerta enviada: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
            else:
                subj = f"🚨 {self.cfg['name']} - CONFIRMADO fuera del aire"
                body = (
                    f"ALERTA VERIFICADA\n\n"
                    f"Se ha confirmado que la estación '{self.cfg['name']}' está fuera del aire.\n\n"
                    f"Tiempo sin audio: {human_duration(total_downtime)}\n"
                    f"Verificado durante: {human_duration(GRACE_PERIOD_SECONDS)}\n"
                    f"Inicio de caída: {self.silence_start_ts.strftime('%Y-%m-%d %H:%M:%S') if self.silence_start_ts else 'N/A'}\n"
                    f"Alerta enviada: {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"Esta alerta se envió después de verificar que el problema persiste.\n"
                )
            
            # ENVIAR EMAIL - Con logging detallado
            logger.info(f"[{self.cfg['name']}] ========== ENVIANDO EMAIL DE ALERTA ==========")
            logger.info(f"[{self.cfg['name']}] Destinatarios: {self.cfg.get('recipients', [])}")
            logger.info(f"[{self.cfg['name']}] Asunto: {subj}")
            logger.info(f"[{self.cfg['name']}] Tiempo fuera: {human_duration(total_downtime)}")
            
            try:
                email_sent = send_mail(self.cfg, subj, body)
                if email_sent:
                    logger.info(f"[{self.cfg['name']}] ✓✓✓ EMAIL DE ALERTA ENVIADO EXITOSAMENTE ✓✓✓")
                else:
                    logger.error(f"[{self.cfg['name']}] ✗✗✗ ERROR: send_mail retornó False ✗✗✗")
                    logger.error(f"[{self.cfg['name']}] Verifica SMTP_USER: {SMTP_USER}")
                    logger.error(f"[{self.cfg['name']}] Verifica SMTP_HOST: {SMTP_HOST}:{SMTP_PORT}")
            except Exception as e:
                logger.error(f"[{self.cfg['name']}] ✗✗✗ EXCEPCIÓN al enviar email: {e} ✗✗✗")
            
            # Push notification
            asyncio.create_task(send_push_async(
                title=subj,
                body=f"Verificado: {human_duration(total_downtime)} sin audio",
                data={
                    "station_id": (self.cfg.get("url","").lower()),
                    "state": "off",
                    "name": self.cfg.get("name",""),
                    "network_issue": str(not self.network_ok),
                    "verified": "true",
                    "downtime": str(total_downtime)
                }
            ))

        # evento OFF
        push_event({
            "type": "state",
            "station_id": self.cfg.get("url","").lower(),
            "name": self.cfg.get("name",""),
            "on_air": False,
            "level_dbfs": float(self.current_db),
            "message": self.lbl_info.value,
            "network_issue": not self.network_ok,
            "verified": True,
            "email_sent": not skip_email
        })
        asyncio.create_task(fs_write_station_if_changed(self))

    def _on_silence_end(self, dur: float):
        """Maneja la recuperación de audio."""
        now = dt.datetime.now()
        
        # Si estaba en periodo de verificación, cancelar alerta
        if self.pending_alert:
            logger.info(f"[{self.cfg['name']}] Recuperación durante verificación - Alerta cancelada (no se envió email)")
            self.pending_alert = False
            self.alert_verification_start = None
            self.silence_start_ts = None
            self.ui_set_status(True)
            self.lbl_info.value = "Conectado"
            self.page.update()
            return
        
        # Si NO había alerta activa confirmada, solo actualizar UI
        if not self.alert_active:
            self.ui_set_status(True)
            self.lbl_info.value = "Conectado"
            self.silence_start_ts = None
            self.page.update()
            return
        
        # Recuperación de una alerta confirmada
        self.ui_set_status(True)
        self.lbl_info.value = "Conectado"
        
        # Calcular downtime real
        if self.confirmed_downtime_start:
            real_downtime = (now - self.confirmed_downtime_start).total_seconds()
        else:
            real_downtime = dur
        
        # Acumular downtime
        self.total_downtime += real_downtime
        
        # === GUARDAR EN HISTORIAL ===
        event = {
            "timestamp": now.isoformat(),
            "station_id": self.cfg.get("url", "").lower(),
            "station_name": self.cfg.get("name", ""),
            "event_type": "online",
            "downtime_duration": real_downtime,
            "level_dbfs": float(self.current_db)
        }
        save_event_to_history(event)
        
        self.page.update()
        
        # ENVIAR EMAIL DE RECUPERACIÓN
        if self.alert_active:
            subj = f"✅ {self.cfg['name']} - Recuperado"
            body = (
                f"RECUPERACIÓN CONFIRMADA\n\n"
                f"La estación '{self.cfg['name']}' ha vuelto al aire.\n\n"
                f"Tiempo fuera del aire: {human_duration(real_downtime)}\n"
                f"Inicio de caída: {self.confirmed_downtime_start.strftime('%Y-%m-%d %H:%M:%S') if self.confirmed_downtime_start else 'N/A'}\n"
                f"Recuperación: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            
            send_mail(self.cfg, subj, body)
            logger.info(f"[{self.cfg['name']}] ✓ Email de recuperación enviado (downtime: {human_duration(real_downtime)})")
            
            # Push notification
            asyncio.create_task(send_push_async(
                title=f"✅ {self.cfg['name']} recuperado",
                body=f"Volvió al aire tras {human_duration(real_downtime)}",
                data={
                    "station_id": (self.cfg.get("url","").lower()),
                    "state": "on",
                    "name": self.cfg.get("name",""),
                    "downtime_duration": str(real_downtime)
                }
            ))
            
            self.alert_active = False
            self.confirmed_downtime_start = None
        
        self.silence_start_ts = None
        
        # evento ON
        push_event({
            "type": "state",
            "station_id": self.cfg.get("url","").lower(),
            "name": self.cfg.get("name",""),
            "on_air": True,
            "level_dbfs": float(self.current_db),
            "message": "Conectado",
        })
        asyncio.create_task(fs_write_station_if_changed(self))

    async def _anim_loop(self):
        # suavizado (ataque rápido, caída lenta) + pico
        amp_s = 0.0
        peak = 0.0

        def clamp01(v: float) -> float:
            return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)

        while not self.stop_flag:
            await asyncio.sleep(0.05)

            # dBFS esperado: 0 .. -80
            db = float(self.current_db)
            db = max(min(db, 0.0), -80.0)

            # amplitud base 0..1 (más “dramática” para que sí baje cuando baja)
            a = clamp01((db + 70.0) / 45.0)   # -70=>0, -25=>1 aprox
            a = a ** 1.8                      # curva: db bajos = casi plano

            # smoothing (ataque/cierre)
            if a > amp_s:
                amp_s = 0.55 * amp_s + 0.45 * a
            else:
                amp_s = 0.92 * amp_s + 0.08 * a

            # pico (para que “respire” con golpes)
            peak = max(peak * 0.93, amp_s)

            # altura final (onda completa + aporte de pico)
            A = (0.10 + 0.26 * amp_s) * self.H
            A += (0.04 + 0.10 * peak) * self.H
            A = min(A, 0.46 * self.H)

            # velocidad según energía
            self.phase += 0.10 + 0.55 * amp_s

            # color por umbral (usa tu db_threshold como referencia)
            th = float(self.cfg.get("db_threshold", -45.0))
            if db <= th:
                base = ft.Colors.RED_700
                deep = ft.Colors.RED_900
            elif db <= th + 6.0:
                base = ft.Colors.AMBER_700
                deep = ft.Colors.AMBER_900
            else:
                base = ft.Colors.GREY_500
                deep = ft.Colors.GREY_700

            # opacidad dinámica (oscuro cuando hay poco nivel)
            op_main = 0.30 + 0.55 * amp_s
            op_glow = 0.06 + 0.18 * amp_s

            if hasattr(self, "paint_main"):
                self.paint_main.color = ft.Colors.with_opacity(op_main, base)
            if hasattr(self, "paint_glow"):
                self.paint_glow.color = ft.Colors.with_opacity(op_glow, deep)

            mid = self.H / 2
            cmds = [cv.Path.MoveTo(0, mid)]

            # igual de barato que antes (step=4 sería aún más barato)
            step = 2
            for x in range(0, self.W + 1, step):
                xr = x / max(1, self.W)
                # mezcla 2 senos para look más “moderno”
                y = mid + A * (
                    0.65 * math.sin(xr * 10.0 + self.phase) +
                    0.35 * math.sin(xr * 22.0 + self.phase * 1.25)
                )
                cmds.append(cv.Path.LineTo(float(x), float(y)))

            self.path.elements = cmds
            if hasattr(self, "path_glow"):
                self.path_glow.elements = cmds

            try:
                self.page.update()
            except Exception:
                break



    async def _update_stats_loop(self):
        """Actualiza el timer y las estadísticas cada segundo."""
        while not self.stop_flag:
            await asyncio.sleep(1.0)
            try:
                now = dt.datetime.now()
                
                # Actualizar timer
                if self.silence_start_ts and not self.on_air:
                    elapsed = (now - self.silence_start_ts).total_seconds()
                    self.lbl_timer.value = f"⏱️ Fuera del aire: {human_duration(elapsed)}"
                elif self.on_air and self.last_valid_audio_ts:
                    uptime = (now - self.session_start_ts).total_seconds()
                    self.lbl_timer.value = f"✅ En aire: {human_duration(uptime)}"
                else:
                    self.lbl_timer.value = ""
                
                # Actualizar estadísticas de sesión
                session_duration = (now - self.session_start_ts).total_seconds()
                if session_duration > 0:
                    uptime_pct = ((session_duration - self.total_downtime) / session_duration * 100)
                    self.lbl_stats.value = f"📊 Sesión: Uptime {uptime_pct:.1f}% | Caídas: {self.downtime_events}"
                
                # Actualizar perfil de horario
                schedule = get_current_schedule_profile()
                if schedule == SCHEDULE_PROFILES["tolerant"]:
                    self.lbl_schedule.value = "🕐 Modo tolerante activo (Dom/Lun 4pm-12am)"
                else:
                    self.lbl_schedule.value = ""
                
                self.page.update()
            except Exception as e:
                logger.error(f"Error actualizando stats: {e}")
                break
    
    async def _rds_update_loop(self):
        """Actualiza la información de RDS/Now Playing inmediatamente al cambiar."""
        if not self.rds_url:
            logger.info(f"[{self.cfg['name']}] No hay URL RDS configurada")
            self.lbl_song.value = ""
            return
        
        logger.info(f"[{self.cfg['name']}] Iniciando loop RDS: {self.rds_url}")
        last_song = None
        
        while not self.stop_flag:
            try:
                # Verificar cada 5 segundos para capturar pisadores cortos
                await asyncio.sleep(5)
                
                # Obtener datos de RDS
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self.rds_url) as response:
                        if response.status == 200:
                            # Leer como texto primero
                            text = await response.text()
                            logger.debug(f"[{self.cfg['name']}] RDS raw: {text[:200]}")
                            
                            # Intentar parsear como JSON
                            try:
                                data = json.loads(text)
                                logger.debug(f"[{self.cfg['name']}] RDS parsed: {data}")
                            except json.JSONDecodeError as e:
                                logger.warning(f"[{self.cfg['name']}] RDS no es JSON válido: {text[:100]} - Error: {e}")
                                continue
                            
                            # Extraer información de canción
                            song_info = "Unknown"
                            
                            if isinstance(data, dict):
                                title = data.get("title", "")
                                artist = data.get("artist", "")
                                
                                logger.debug(f"[{self.cfg['name']}] RDS title={title}, artist={artist}")
                                
                                # Construir string de canción
                                if artist and title:
                                    song_info = f"{artist} - {title}"
                                elif title:
                                    song_info = title
                                elif artist:
                                    song_info = artist
                                
                                # Fallbacks
                                if song_info == "Unknown":
                                    if "current_track" in data:
                                        track = data["current_track"]
                                        if isinstance(track, dict):
                                            song_info = track.get("title", track.get("text", "Unknown"))
                                        elif isinstance(track, str):
                                            song_info = track
                                    elif "nowplaying" in data:
                                        song_info = data["nowplaying"]
                                    elif "song" in data:
                                        song_info = data["song"].get("text", data["song"]) if isinstance(data["song"], dict) else data["song"]
                            
                            # Limpiar y actualizar SOLO SI CAMBIÓ
                            song_info = str(song_info).strip()
                            if song_info and song_info != "Unknown" and song_info != last_song:
                                self.current_song = song_info
                                last_song = song_info
                                
                                logger.info(f"[{self.cfg['name']}] ✓ RDS actualizado: {song_info}")
                                
                                # Guardar en historial INMEDIATAMENTE
                                save_rds_entry(self.cfg["name"], song_info)
                                
                                # Actualizar UI (SIN 🚨 en UI principal)
                                self.lbl_song.value = f"♪ {song_info}"
                                self.lbl_song.color = ft.Colors.CYAN_400
                                
                                self.page.update()
                            elif song_info == last_song:
                                logger.debug(f"[{self.cfg['name']}] RDS sin cambios: {song_info}")
                        else:
                            logger.warning(f"[{self.cfg['name']}] RDS HTTP {response.status}")
                            
            except asyncio.TimeoutError:
                logger.warning(f"[{self.cfg['name']}] RDS timeout")
            except Exception as e:
                logger.error(f"[{self.cfg['name']}] Error RDS: {e}", exc_info=True)
        """Actualiza la información de RDS/Now Playing cada X segundos."""
        if not self.rds_url:
            logger.info(f"[{self.cfg['name']}] No hay URL RDS configurada")
            self.lbl_song.value = ""
            return
        
        logger.info(f"[{self.cfg['name']}] Iniciando loop RDS: {self.rds_url}")
        last_song = None
        
        while not self.stop_flag:
            try:
                await asyncio.sleep(RDS_UPDATE_INTERVAL)
                
                # Obtener datos de RDS con content_type text para evitar error de mimetype
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self.rds_url) as response:
                        if response.status == 200:
                            # Leer como texto primero
                            text = await response.text()
                            logger.debug(f"[{self.cfg['name']}] RDS raw: {text[:200]}")
                            
                            # Intentar parsear como JSON
                            try:
                                data = json.loads(text)
                                logger.debug(f"[{self.cfg['name']}] RDS parsed: {data}")
                            except json.JSONDecodeError as e:
                                logger.warning(f"[{self.cfg['name']}] RDS no es JSON válido: {text[:100]} - Error: {e}")
                                continue
                            
                            # Extraer información de canción
                            # Formato de onair.radioapi.io: {"title":"...","artist":"...","duration":"..."}
                            song_info = "Unknown"
                            
                            if isinstance(data, dict):
                                title = data.get("title", "")
                                artist = data.get("artist", "")
                                
                                logger.debug(f"[{self.cfg['name']}] RDS title={title}, artist={artist}")
                                
                                # Construir string de canción
                                if artist and title:
                                    song_info = f"{artist} - {title}"
                                elif title:
                                    song_info = title
                                elif artist:
                                    song_info = artist
                                
                                # Fallbacks para otros formatos posibles
                                if song_info == "Unknown":
                                    if "current_track" in data:
                                        track = data["current_track"]
                                        if isinstance(track, dict):
                                            song_info = track.get("title", track.get("text", "Unknown"))
                                        elif isinstance(track, str):
                                            song_info = track
                                    elif "nowplaying" in data:
                                        song_info = data["nowplaying"]
                                    elif "song" in data:
                                        song_info = data["song"].get("text", data["song"]) if isinstance(data["song"], dict) else data["song"]
                            
                            # Limpiar y actualizar
                            song_info = str(song_info).strip()
                            if song_info and song_info != "Unknown" and song_info != last_song:
                                self.current_song = song_info
                                last_song = song_info
                                
                                logger.info(f"[{self.cfg['name']}] ✓ RDS actualizado: {song_info}")
                                
                                # Guardar en historial
                                save_rds_entry(self.cfg["name"], song_info)
                                
                                # Verificar patrón sospechoso
                                self.song_is_suspicious = check_suspicious_pattern(song_info, self.cfg["name"])
                                
                                # Actualizar UI con color según si es sospechoso
                                if self.song_is_suspicious:
                                    self.lbl_song.value = f"🚨 ♪ {song_info}"
                                    self.lbl_song.color = ft.Colors.RED_400
                                    logger.warning(f"[{self.cfg['name']}] Patrón sospechoso: {song_info}")
                                else:
                                    self.lbl_song.value = f"♪ {song_info}"
                                    self.lbl_song.color = ft.Colors.CYAN_400
                                
                                self.page.update()
                            elif song_info == last_song:
                                logger.debug(f"[{self.cfg['name']}] RDS sin cambios: {song_info}")
                        else:
                            logger.warning(f"[{self.cfg['name']}] RDS HTTP {response.status}")
                            
            except asyncio.TimeoutError:
                logger.warning(f"[{self.cfg['name']}] RDS timeout")
            except Exception as e:
                logger.error(f"[{self.cfg['name']}] Error RDS: {e}", exc_info=True)
            
            await asyncio.sleep(max(0, RDS_UPDATE_INTERVAL - 1))

    def stop(self):
        self.stop_flag = True
        self._kill_ffmpeg()
        self._stop_audio()
        for t in (self.ffmpeg_task, self.rds_task, self.anim_task, self.stats_task, self.keepalive_task):
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
        self.ffmpeg_task = self.rds_task = self.anim_task = self.stats_task = self.keepalive_task = None


app_state = {"cfg": {}, "stations_ui": [], "stations_col": None}

def main(page: ft.Page):
    page.title = APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0  # Sin padding para llenar toda la pantalla
    page.scroll = ft.ScrollMode.HIDDEN  # Sin scroll
    page.spacing = 0  # Sin spacing entre elementos
    
    # CRÍTICO: Forzar que la página use todo el espacio disponible
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.expand = True  # La página misma debe expandirse

    cfg = load_cfg()
    app_state["cfg"] = cfg

    # Container principal que ocupará TODA la pantalla
    monitors_view = ft.Container(
        expand=True, 
        visible=False,
        padding=10
    )
    
    # Layout 2x2 - Envolver cada Row en Container para forzar expansión vertical
    row1 = ft.Row([], spacing=16)
    row2 = ft.Row([], spacing=16)
    
    # Contenedores que fuerzan cada fila a ocupar 50% de altura
    container1 = ft.Container(content=row1, expand=True)
    container2 = ft.Container(content=row2, expand=True)
    
    # Column que contiene los dos contenedores
    stations_col = ft.Column(
        [container1, container2],
        spacing=16,
        expand=True
    )
    
    # Asignar al monitors_view
    monitors_view.content = stations_col
    
    # Guardar referencias
    app_state["stations_col"] = stations_col
    app_state["station_row1"] = row1
    app_state["station_row2"] = row2
    app_state["station_count"] = 0

    page.add(monitors_view)

    # === COMPARADOR DE ESTACIONES ===
    def show_comparator(_=None):
        """Muestra una vista comparativa de todas las estaciones."""
        stations = app_state.get("stations_ui", [])
        if not stations:
            snack(page, "No hay estaciones para comparar")
            return
        
        overlay = ft.Container(expand=True)
        
        def close_comparator(_=None):
            try:
                page.overlay.remove(overlay)
            except Exception:
                pass
            page.update()
        
        # Crear tabla comparativa
        rows = []
        for ui in stations:
            now = dt.datetime.now()
            
            # Calcular tiempo en estado actual
            if ui.on_air:
                time_in_state = (now - (ui.silence_start_ts or ui.session_start_ts)).total_seconds()
                state_text = f"ON {human_duration(time_in_state)}"
                state_color = ft.Colors.GREEN_400
            else:
                time_in_state = (now - ui.silence_start_ts).total_seconds() if ui.silence_start_ts else 0
                state_text = f"OFF {human_duration(time_in_state)}"
                state_color = ft.Colors.RED_400
            
            # Calcular uptime de sesión
            session_duration = (now - ui.session_start_ts).total_seconds()
            uptime_pct = ((session_duration - ui.total_downtime) / session_duration * 100) if session_duration > 0 else 100.0
            
            row = ft.Row(
                [
                    ft.Container(
                        content=ft.Text(ui.cfg["name"], size=14, weight=ft.FontWeight.W_500),
                        width=200
                    ),
                    ft.Container(
                        content=ft.Row([
                            ft.Container(width=12, height=12, bgcolor=state_color, border_radius=6),
                            ft.Text(state_text, size=12)
                        ], spacing=8),
                        width=150
                    ),
                    ft.Container(
                        content=ft.Text(f"{ui.current_db:.1f} dBFS", size=12),
                        width=100
                    ),
                    ft.Container(
                        content=ft.Text(f"{uptime_pct:.1f}%", size=12),
                        width=80
                    ),
                    ft.Container(
                        content=ft.Text(f"{ui.downtime_events}", size=12),
                        width=80
                    ),
                    ft.Container(
                        content=ft.Text("⚠️ Red" if not ui.network_ok else "✓", size=12),
                        width=60
                    ),
                ],
                alignment=ft.MainAxisAlignment.START
            )
            rows.append(row)
        
        # Header de la tabla
        header = ft.Row(
            [
                ft.Container(
                    content=ft.Text("Estación", size=14, weight=ft.FontWeight.W_700),
                    width=200
                ),
                ft.Container(
                    content=ft.Text("Estado", size=14, weight=ft.FontWeight.W_700),
                    width=150
                ),
                ft.Container(
                    content=ft.Text("Nivel", size=14, weight=ft.FontWeight.W_700),
                    width=100
                ),
                ft.Container(
                    content=ft.Text("Uptime", size=14, weight=ft.FontWeight.W_700),
                    width=80
                ),
                ft.Container(
                    content=ft.Text("Caídas", size=14, weight=ft.FontWeight.W_700),
                    width=80
                ),
                ft.Container(
                    content=ft.Text("Red", size=14, weight=ft.FontWeight.W_700),
                    width=60
                ),
            ],
            alignment=ft.MainAxisAlignment.START
        )
        
        comparator_content = ft.Column(
            [
                ft.Row([
                    ft.Text("Comparador de Estaciones", size=20, weight=ft.FontWeight.W_700),
                    ft.Container(expand=True),
                    ft.IconButton(icon=ft.Icons.CLOSE, on_click=close_comparator)
                ]),
                ft.Divider(),
                header,
                ft.Divider(),
                *rows
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO
        )
        
        panel = ft.Container(
            width=800,
            padding=20,
            bgcolor=ft.Colors.BLUE_GREY_900,
            border_radius=16,
            content=comparator_content
        )
        
        scrim = ft.Container(
            expand=True,
            bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK),
            on_click=close_comparator
        )
        
        overlay.content = ft.Stack(
            controls=[
                scrim,
                ft.Container(expand=True, alignment=ft.alignment.center, content=panel)
            ]
        )
        
        page.overlay.append(overlay)
        page.update()

    # Campos de entrada
    name_tf = ft.TextField(label="Nombre de la estación", value="", width=420)
    url_tf = ft.TextField(label="URL del stream", value="", width=420)

    def pick_name(v): name_tf.value = v; page.update()
    def pick_url(v): url_tf.value = v; page.update()

    name_hist = history_button(cfg.get("history_names", []), pick_name)
    url_hist = history_button(cfg.get("history_urls", []), pick_url)

    def start_monitoring(_=None):
        name = (name_tf.value or "").strip()
        url = (url_tf.value or "").strip()
        if not name or not (url.startswith("http://") or url.startswith("https://")):
            snack(page, "Nombre/URL inválidos"); return

        _unique_push(cfg["history_names"], name)
        _unique_push(cfg["history_urls"], url)
        save_cfg(cfg)

        monitors_view.visible = True
        stations_col.visible = True

        page.appbar = ft.AppBar(
            title=ft.Text("Monitoreo de estaciones"),
            actions=[
                ft.IconButton(icon=ft.Icons.ADD, tooltip="+ Estación", on_click=open_add_sheet),
                ft.IconButton(
                    icon=ft.Icons.COMPARE_ARROWS,
                    tooltip="Comparar estaciones",
                    on_click=show_comparator,
                ),
                ft.IconButton(
                    icon=ft.Icons.CLOUD_UPLOAD_OUTLINED,
                    tooltip="Publicar todas a Firebase",
                    on_click=lambda e: page.run_task(fs_publish_all, app_state.get("stations_ui", [])),
                ),
                ft.IconButton(
                    icon=ft.Icons.TERMINAL,
                    tooltip="Ver logs (consola)",
                    on_click=lambda e: open_logs_console(),
                ),
                ft.PopupMenuButton(
                    icon=ft.Icons.MORE_VERT,
                    items=[
                        ft.PopupMenuItem(text="Ver logs (consola)", on_click=lambda e: open_logs_console()),
                        ft.PopupMenuItem(text="Abrir carpeta de logs", on_click=lambda e: open_logs_folder()),
                    ],
                ),
            ],
        )
        page.update()

        stations_col.controls.clear()
        app_state["stations_ui"].clear()

        st = DEFAULT_STATION.copy()
        st["name"], st["url"] = name, url
        nl = name.lower()
        if "puebla" in nl:   st["from"] = "Off-Air Detector Puebla <rboffairdetector@gmail.com>"
        if "tehuac" in nl:   st["from"] = "Off-Air Detector Tehuacán <rboffairdetector@gmail.com>"
        if "lobo"  in nl:    st["from"] = "Off-Air Detector Lobo <rboffairdetector@gmail.com>"

        _apply_saved_prefs(cfg, st)

        ui = StationMonitor(page, st)
        app_state["stations_ui"].append(ui)
        stations_col.controls.append(ui.card)

        page.update()
        ui.start()
       
        _store_prefs(cfg, st)

        setup_wrap.visible = False
        page.update()
        snack(page, "Monitoreo iniciado")

        # escritura inicial en Firestore
        page.run_task(fs_write_station, ui)

    # Sheet para agregar estación (compacto)
    def open_add_sheet(_=None):
        n = ft.TextField(label="Nombre de la estación", width=420)
        u = ft.TextField(label="URL del stream", width=420)
        def pick_n(v): n.value = v; page.update()
        def pick_u(v): u.value = v; page.update()
        n_hist = history_button(cfg.get("history_names", []), pick_n)
        u_hist = history_button(cfg.get("history_urls",  []), pick_u)

        overlay = ft.Container(expand=True)

        def close_sheet(_=None):
            try:
                page.overlay.remove(overlay)
            except Exception:
                pass
            page.update()

        def add_now(_=None):
            name = (n.value or "").strip()
            url  = (u.value or "").strip()
            if not name or not (url.startswith("http://") or url.startswith("https://")):
                snack(page, "Nombre/URL inválidos"); return

            _unique_push(cfg["history_names"], name)
            _unique_push(cfg["history_urls"], url)
            save_cfg(cfg)

            st = DEFAULT_STATION.copy()
            st["name"], st["url"] = name, url
            nl = name.lower()
            if "puebla" in nl:   st["from"] = "Off-Air Detector Puebla <rboffairdetector@gmail.com>"
            if "tehuac" in nl:   st["from"] = "Off-Air Detector Tehuacán <rboffairdetector@gmail.com>"
            if "lobo"  in nl:    st["from"] = "Off-Air Detector Lobo <rboffairdetector@gmail.com>"

            _apply_saved_prefs(cfg, st)

            ui = StationMonitor(page, st)
            app_state["stations_ui"].append(ui)
            
            # Las cards ya tienen expand=True en su definición
            
            # Agregar a fila 1 o fila 2 según contador
            count = app_state.get("station_count", 0)
            if count < 2:
                app_state["station_row1"].controls.append(ui.card)
            else:
                app_state["station_row2"].controls.append(ui.card)
            app_state["station_count"] = count + 1

            page.update()
            ui.start()

            _store_prefs(cfg, st)
            close_sheet()
            # escritura inicial a Firestore para esta estación
            page.run_task(fs_write_station, ui)

        header = ft.Row(
            [
                ft.Text("Agregar estación", size=18, weight=ft.FontWeight.W_600),
                ft.Container(expand=True),
                ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Cerrar", on_click=close_sheet),
            ],
            alignment=ft.MainAxisAlignment.START,
        )

        panel = ft.Container(
            width=520, padding=20, bgcolor=ft.Colors.BLUE_GREY_900, border_radius=16,
            content=ft.Column(
                [
                    header,
                    ft.Row([n, n_hist], alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                    ft.Row([u, u_hist], alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                    ft.Row([ft.FilledButton("Agregar", on_click=add_now)],
                        alignment=ft.MainAxisAlignment.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12, tight=True
            )
        )

        scrim = ft.Container(expand=True,
                            bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK),
                            on_click=close_sheet)

        overlay.content = ft.Stack(
            controls=[
                scrim,
                ft.Container(expand=True, alignment=ft.alignment.center, content=panel)
            ]
        )
        page.overlay.append(overlay)
        page.update()

    # Tarjeta de bienvenida (centrada)
    setup_card = ft.Container(
        border_radius=16, padding=24, bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE),
        content=ft.Column(
            [
                ft.Text("Configurar estación", size=20, weight=ft.FontWeight.W_600, text_align=ft.TextAlign.CENTER),
                ft.Row([name_tf, name_hist], alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                ft.Row([url_tf,  url_hist],  alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                ft.Row([ft.FilledButton("Comenzar monitoreo", on_click=start_monitoring)],
                       alignment=ft.MainAxisAlignment.CENTER),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12
        ),
        width=520
    )
    setup_wrap = ft.Container(expand=True, alignment=ft.alignment.center, content=setup_card, visible=True)
    page.add(setup_wrap)
    
    # === CARGAR AUTOMÁTICAMENTE ESTACIONES GUARDADAS ===
    # Si ya hay estaciones configuradas, cargarlas automáticamente
    if cfg.get("stations"):
        setup_wrap.visible = False
        monitors_view.visible = True
        
        row1 = app_state["station_row1"]
        row2 = app_state["station_row2"]
        count = 0
        
        for url, st in cfg["stations"].items():
            try:
                # IMPORTANTE: Asegurar que la URL esté en la config
                if "url" not in st:
                    st["url"] = url
                
                ui = StationMonitor(page, st)
                app_state["stations_ui"].append(ui)
                
                # Las cards ya tienen expand=True en su definición
                # Hacer que cada card tenga altura fija del 50% de la ventana
                ui.card.height = None  # Quitar altura fija
                
                # Agregar a fila 1 o fila 2
                if count < 2:
                    row1.controls.append(ui.card)
                else:
                    row2.controls.append(ui.card)
                
                count += 1
                logger.info(f"Cargando estación: {st.get('name', 'Unknown')} - URL: {st.get('url', 'No URL')}")
            except Exception as e:
                logger.error(f"Error cargando estación {st.get('name', 'Unknown')}: {e}")
        
        app_state["station_count"] = count
        page.update()
        
        # Iniciar monitoreo de todas las estaciones cargadas
        for ui in app_state["stations_ui"]:
            ui.start()
    
    page.update()
    
    async def _startup_tasks(_page: ft.Page):
        # arranca API HTTP/WS en background
        asyncio.create_task(start_api_server())

    page.run_task(_startup_tasks, page)

# ---------- API HTTP/WS ----------
async def api_get_stations(request: web.Request):
    lst = [station_to_json(ui) for ui in app_state.get("stations_ui", [])]
    return web.json_response(lst)

async def api_get_events(request: web.Request):
    since = request.query.get("since")
    items = list(events_deque)
    if since:
        try:
            t0 = dt.datetime.fromisoformat(since)
            items = [e for e in items if dt.datetime.fromisoformat(e["ts"]) >= t0]
        except Exception:
            pass
    return web.json_response(items)

async def api_publish(request: web.Request):
    await fs_publish_all(app_state.get("stations_ui", []))
    return web.json_response({"ok": True})

async def api_ws(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    ws_clients.add(ws)
    try:
        # opcional: manda un hello
        await ws.send_str(json.dumps({"type":"hello","ts":dt.datetime.now().isoformat()}))
        async for _ in ws:
            pass
    finally:
        ws_clients.discard(ws)
    return ws

async def start_api_server():
    app = web.Application()
    app.router.add_get("/api/v1/stations", api_get_stations)
    app.router.add_get("/api/v1/events", api_get_events)
    app.router.add_get("/api/v1/stream", api_ws)
    app.router.add_post("/api/v1/publish", api_publish)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    logger.info(f"API levantada en http://{API_HOST}:{API_PORT}")


ft.app(target=main)
