# Monitor Al Aire

Vigila los streams de las estaciones y avisa por **Telegram** cuando una estación
se sale del aire. Es un programa de consola, ligero: solo necesita Python 3.10+
y `ffmpeg` (sin numpy, sin Flet, sin Firebase).

## Cómo decide que una estación está fuera del aire

- Cada estación puede tener varios streams (por ejemplo, principal y respaldo).
- Mide el nivel de audio de cada stream cada segundo.
- La estación está **FUERA DEL AIRE** solo si **ningún** stream tiene audio
  durante `alerta_tras_segundos` (90 s por defecto). Puede ser por silencio o
  porque no hay conexión.
- Para avisar que **volvió al aire** se necesitan `recuperacion_segundos` de audio
  continuo (15 s por defecto). El estado sale del audio que se mide, así que la
  recuperación siempre se detecta.
- Si solo **un** stream se cae y otro sigue con audio, manda un aviso aparte
  (⚠️) después de `aviso_stream_tras_segundos` (5 min). Con `0` no se manda.
- Mientras la estación siga fuera, manda un recordatorio cada
  `recordatorio_minutos` (con `0` no se manda).
- Si la PC del monitor se queda **sin internet** o se suspende, no manda
  alertas falsas. Al volver avisa que el monitor estuvo sin conexión.

## Instalación (Windows)

1. Instala Python 3 (marca "Add to PATH").
2. Pon `ffmpeg.exe` en la misma carpeta que `monitor_aire.py` (o en el PATH).
3. Crea el bot de Telegram:
   - En Telegram, habla con **@BotFather**, manda `/newbot` y copia el **token**.
   - Mándale cualquier mensaje a tu bot nuevo.
   - Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador y copia
     el número `"chat":{"id": ...}`. Ese es tu **chat_id**. Para un grupo, agrega
     el bot al grupo, escribe algo y usa el id del grupo (empieza con `-`).
4. Copia `config_monitor.ejemplo.json` como `config_monitor.json` y llénalo
   con el token, el chat_id y las URLs de los 2 streams de cada estación.
   Puedes poner `umbral_db`, `alerta_tras_segundos`, etc. dentro de una
   estación para cambiarlos solo en esa estación. `umbral_db` también se puede
   poner dentro de un stream.
5. Haz doble clic en `iniciar_monitor.bat`. Si el programa se cierra, el .bat
   lo vuelve a abrir solo.

Para que arranque con Windows, pon un acceso directo a `iniciar_monitor.bat`
en `shell:startup`. También conviene desactivar la suspensión de la PC.

## Comandos de Telegram

- `/estado`: muestra el estado y el nivel de cada stream de cada estación.
- `/ayuda`: muestra la lista de comandos.

## Ajustes recomendados

| Parámetro | Defecto | Notas |
|---|---|---|
| `umbral_db` | -50 | Por debajo de este nivel cuenta como silencio. Súbelo a -45 si el ruido de fondo de un stream "muerto" no baja lo suficiente. |
| `alerta_tras_segundos` | 90 | Bájalo para enterarte antes. Súbelo si hay programas con pausas largas. |
| `recuperacion_segundos` | 15 | |
| `aviso_stream_tras_segundos` | 300 | |
| `recordatorio_minutos` | 30 | |

El log se guarda en `monitor_aire.log`.

## Crear un .exe (opcional)

```
pip install pyinstaller
pyinstaller --onefile --name MonitorAlAire monitor_aire.py
```

Pon `MonitorAlAire.exe`, `ffmpeg.exe` y `config_monitor.json` en la misma carpeta.

---

`monitor_radio.py` es el programa anterior (con interfaz Flet, correo, Firebase y RDS).
Ya no hace falta para el monitoreo.
