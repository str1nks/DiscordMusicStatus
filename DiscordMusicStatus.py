"""
Discord Music Status

Два процесса в одном файле:
  * фоновый демон (python w.py --daemon): держит Discord Rich Presence,
    работает без окна, переживает закрытие терминала и стартует вместе с Windows;
  * терминальное меню (python w.py): подключается к демону и управляет им.
    Если демон не запущен, меню само его запустит.

Данные (пресеты, текущий режим, лог) лежат в %APPDATA%\\DiscordMusicStatus.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from pypresence import Presence, ActivityType
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

# ------------------------------------------------------------------ настройки

CLIENT_ID = "00000000000000000"

# Какие приложения считаем Яндекс Музыкой (часть названия, маленькими буквами).
APP_KEYWORDS = ["ru.yandex.desktop.music"]

# Картинка по умолчанию, если обложка не нашлась
DEFAULT_COVER = "https://cdn-yc-static.i-m-i.ru/store/uploads/profile/603/photo/main-77dcb90b3c7b427e015da6a4693bc1f6.jpg"

# Локальный порт связи меню с демоном (только 127.0.0.1).
# Если он у тебя чем-то занят, поменяй на любой другой свободный.
PORT = 47653

APP_NAME = "DiscordMusicStatus"
SCRIPT = Path(__file__).resolve()
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "daemon.log"

DEFAULT_CONFIG = {
    "mode": "auto",  # auto | preset | off
    "preset": "",
    "presets": [
        {
            "name": "Нервы - Нервы",
            "title": "Нервы",
            "artist": "Нервы",
            "duration": 158,
            "cover": "",
        },
    ],
}

# ------------------------------------------------------------------ общее


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key in cfg:
            if key in data:
                cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_preset(p):
    try:
        duration = int(p.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "name": str(p.get("name", "")).strip(),
        "title": str(p.get("title", "")).strip(),
        "artist": str(p.get("artist", "")).strip(),
        "duration": max(duration, 0),
        "cover": str(p.get("cover", "")).strip(),
    }


cover_cache = {}


def find_cover(artist, title):
    """Ищет обложку через публичный поиск iTunes. Не нашлось: картинка по умолчанию."""
    key = (artist, title)
    if key in cover_cache:
        return cover_cache[key]
    url = None
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "entity": "song", "limit": 1},
            timeout=5,
        )
        results = r.json().get("results")
        if results:
            url = results[0]["artworkUrl100"].replace("100x100bb", "600x600bb")
    except Exception:
        pass
    cover_cache[key] = url or DEFAULT_COVER
    return cover_cache[key]


async def get_track():
    manager = await MediaManager.request_async()
    for session in manager.get_sessions():
        app = session.source_app_user_model_id.lower()
        if not any(k in app for k in APP_KEYWORDS):
            continue
        if session.get_playback_info().playback_status != PlaybackStatus.PLAYING:
            continue
        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        duration = (timeline.end_time - timeline.start_time).total_seconds()
        position = timeline.position.total_seconds() - timeline.start_time.total_seconds()
        position = current_position(timeline, position, duration)
        return {
            "title": props.title,
            "artist": props.artist,
            "position": position,
            "duration": duration,
        }
    return None


def current_position(timeline, position, duration):
    """Реальная позиция трека прямо сейчас.

    Windows отдаёт position как снимок на момент timeline.last_updated_time, а
    плеер обновляет его нечасто (например, только при смене трека, паузе или
    перемотке). Если брать position как есть, то «начало трека» (now - position)
    каждый опрос уезжает вперёд на длину паузы между опросами, и полоска
    в Discord сбрасывается. Поэтому прибавляем время, прошедшее с момента снимка.
    """
    try:
        updated = timeline.last_updated_time
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - updated).total_seconds()
        # мусорные значения (дата 1601 года и т.п.) и отрицательные не берём
        if 0 < elapsed < 86400:
            position += elapsed
    except Exception:
        pass
    if duration > 0:
        position = min(position, duration)
    return max(position, 0.0)


def is_gui_exe(path):
    """True, если exe оконный (subsystem GUI), то есть не открывает консоль."""
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            pe = int.from_bytes(f.read(4), "little")
            f.seek(pe + 92)
            return int.from_bytes(f.read(2), "little") == 2
    except OSError:
        return False


def pythonw_path():
    """Чем запускать фоновый процесс: только оконный exe, чтобы не появлялась консоль.

    Сначала DiscordMusicStatus.exe (твоя копия pythonw.exe, если она оконная),
    потом обычный pythonw.exe, в крайнем случае python.exe (окно скроется само).
    """
    exe = Path(sys.executable)
    for name in ("DiscordMusicStatus.exe", "pythonw.exe"):
        candidate = exe.with_name(name)
        if candidate.exists() and is_gui_exe(candidate):
            return str(candidate)
    return str(exe)


FROZEN = getattr(sys, "frozen", False)  # True, когда запущено из собранного .exe


def daemon_command():
    """Команда запуска фонового процесса (списком для subprocess)."""
    if FROZEN:
        # рядом лежит безоконная копия DiscordMusicStatusBg.exe
        bg = Path(sys.executable).with_name(APP_NAME + "Bg.exe")
        return [str(bg if bg.exists() else Path(sys.executable)), "--daemon"]
    return [pythonw_path(), str(SCRIPT), "--daemon"]


def hide_console():
    """Если у фонового процесса всё же есть окно консоли, прячем его."""
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


# ------------------------------------------------------------------ демон


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.config = load_config()
        self.info = "запускаюсь..."
        self.connected = False
        self.stop = threading.Event()
        self.wake = threading.Event()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.config))

    def update(self, new):
        with self.lock:
            if new.get("mode") in ("auto", "preset", "off"):
                self.config["mode"] = new["mode"]
            if "preset" in new:
                self.config["preset"] = str(new["preset"])
            if isinstance(new.get("presets"), list):
                self.config["presets"] = [
                    clean_preset(p) for p in new["presets"] if isinstance(p, dict) and p.get("name")
                ]
            save_config(self.config)
        self.wake.set()

    def sleep(self, seconds):
        """Спим, но просыпаемся сразу, если пришла новая команда."""
        self.wake.wait(seconds)
        self.wake.clear()


def worker(state):
    winrt_loop = asyncio.new_event_loop()
    rpc = None
    applied = None  # что сейчас показано: "off" / "auto" / ключ пресета
    last = None  # последний показанный трек в авто-режиме
    started = 0
    duration = 0
    cover = DEFAULT_COVER

    def drop():
        nonlocal rpc, applied, last
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass
        rpc = None
        applied = None
        last = None
        state.connected = False

    while not state.stop.is_set():
        cfg = state.snapshot()
        mode = cfg["mode"]
        preset = None
        if mode == "preset":
            preset = next((p for p in cfg["presets"] if p["name"] == cfg["preset"]), None)
            if preset is None:
                mode = "off"

        delay = 3
        try:
            if mode == "off":
                if rpc is not None and applied != "off":
                    rpc.clear()
                applied = "off"
                last = None
                state.info = "статус выключен"
            else:
                if rpc is None:
                    rpc = Presence(CLIENT_ID)
                    rpc.connect()
                    state.connected = True
                    applied = None
                    logging.info("Подключился к Discord")

                if mode == "auto":
                    if applied != "auto":
                        if applied is not None:
                            rpc.clear()
                        applied = "auto"
                        last = None
                    track = winrt_loop.run_until_complete(get_track())
                    if track is None or not track["title"]:
                        if last is not None:
                            rpc.clear()
                            last = None
                        state.info = "в Яндекс Музыке ничего не играет"
                    else:
                        start = time.time() - track["position"]
                        changed = (
                            last is None
                            or last["title"] != track["title"]
                            or last["artist"] != track["artist"]
                            or abs(start - last["start"]) > 5  # перемотка (допуск гасит дрожание позиции)
                        )
                        if changed:
                            kwargs = dict(
                                activity_type=ActivityType.LISTENING,
                                details=track["title"],
                                state=track["artist"] or "Unknown",
                                large_image=find_cover(track["artist"], track["title"]),
                                large_text=track["title"],
                                start=int(start),
                            )
                            if track["duration"] > 0:
                                kwargs["end"] = int(start + track["duration"])
                            rpc.update(**kwargs)
                            last = {**track, "start": start}
                        state.info = f"{track['artist'] or 'Unknown'} - {track['title']}"
                    delay = 3

                else:  # preset
                    key = "preset:" + json.dumps(preset, sort_keys=True)
                    now = time.time()
                    new_preset = applied != key
                    expired = duration > 0 and now >= started + duration
                    if new_preset or expired:
                        if new_preset:
                            cover = preset["cover"] or find_cover(preset["artist"], preset["title"])
                            duration = preset["duration"]
                        started = int(now)
                        kwargs = dict(
                            activity_type=ActivityType.LISTENING,
                            details=preset["title"],
                            state=preset["artist"] or "Unknown",
                            large_image=cover,
                            large_text=preset["title"],
                            start=started,
                        )
                        if duration > 0:
                            kwargs["end"] = started + duration
                        rpc.update(**kwargs)
                        applied = key
                    state.info = f"пресет: {preset['artist'] or 'Unknown'} - {preset['title']}"
                    delay = 1
        except Exception as e:
            logging.warning("Ошибка: %s", e)
            drop()
            state.info = "Discord недоступен, пробую снова..."
            delay = 5

        state.sleep(delay)

    if rpc is not None:
        try:
            rpc.clear()
            rpc.close()
        except Exception:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False  # на Windows True позволил бы запустить второй демон
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        state = server.state
        try:
            req = json.loads(self.rfile.readline().decode("utf-8"))
            cmd = req.get("cmd")
            if cmd == "get":
                resp = {
                    "ok": True,
                    "config": state.snapshot(),
                    "info": state.info,
                    "connected": state.connected,
                }
            elif cmd == "set":
                state.update(req.get("config", {}))
                resp = {"ok": True}
            elif cmd == "stop":
                state.stop.set()
                state.wake.set()
                threading.Thread(target=server.shutdown, daemon=True).start()
                resp = {"ok": True}
            else:
                resp = {"ok": False, "error": "unknown command"}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))


def run_daemon():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")

    # под pythonw нет stdout/stderr
    hide_console()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        logging.info("Демон уже запущен, выхожу")
        return

    state = State()
    server.state = state
    t = threading.Thread(target=worker, args=(state,))
    t.start()
    logging.info("Демон запущен")
    try:
        server.serve_forever()
    finally:
        state.stop.set()
        state.wake.set()
        t.join(timeout=10)
        server.server_close()
        logging.info("Демон остановлен")


# ------------------------------------------------------------------ связь с демоном


def request(cmd, timeout=3, **data):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
            s.sendall((json.dumps({"cmd": cmd, **data}) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode("utf-8"))
    except (OSError, ValueError):
        return None


def ensure_daemon():
    if request("get"):
        return True
    print("Запускаю фоновый процесс...")
    flags = 0
    startupinfo = None
    if os.name == "nt":
        # CREATE_NO_WINDOW, а не DETACHED_PROCESS: с DETACHED venv-лаунчер python.exe
        # порождал дочерний процесс с новым видимым окном консоли
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    subprocess.Popen(
        daemon_command(),
        creationflags=flags,
        startupinfo=startupinfo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    for _ in range(40):
        time.sleep(0.25)
        if request("get"):
            return True
    return False


# ------------------------------------------------------------------ автозапуск

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    return subprocess.list2cmdline(daemon_command())


def autostart_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    if winreg is None:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ терминальное меню


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def fmt_duration(sec):
    return f"{sec // 60}:{sec % 60:02d}" if sec else "без полоски"


def parse_duration(text, fallback=0):
    text = text.strip()
    if not text:
        return fallback
    try:
        if ":" in text:
            minutes, seconds = text.split(":")
            return int(minutes) * 60 + int(seconds)
        return int(text)
    except ValueError:
        print("Не понял длительность, оставляю как было.")
        return fallback


def mode_label(cfg):
    if cfg["mode"] == "auto":
        return "Авто (Яндекс Музыка)"
    if cfg["mode"] == "preset" and any(p["name"] == cfg["preset"] for p in cfg["presets"]):
        return f"Пресет «{cfg['preset']}»"
    return "Выключен"


def pick_preset(presets, prompt="Номер пресета (Enter: отмена)"):
    if not presets:
        print("Пресетов пока нет. Добавь их в пункте «Пресеты».")
        input("Enter...")
        return None
    for i, p in enumerate(presets, 1):
        print(f"  {i}. {p['name']}  ({p['artist']} - {p['title']}, {fmt_duration(p['duration'])})")
    choice = input(f"{prompt}: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(presets):
        return presets[int(choice) - 1]
    return None


def form_preset(old=None):
    old = old or {}
    name = ask("Название пресета (как в списке)", old.get("name", ""))
    if not name:
        print("Название не может быть пустым.")
        return None
    title = ask("Название трека", old.get("title", "")) or name
    artist = ask("Исполнитель", old.get("artist", "")) or "Unknown"
    duration = parse_duration(
        ask("Длительность, например 2:38 (Enter: оставить; 0: без полоски)", fmt_duration_input(old.get("duration", 0))),
        old.get("duration", 0),
    )
    cover = ask("Обложка: прямая ссылка на картинку (пусто: искать автоматически)", old.get("cover", ""))
    return {"name": name, "title": title, "artist": artist, "duration": duration, "cover": cover}


def fmt_duration_input(sec):
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def manage_presets():
    while True:
        r = request("get")
        if not r:
            return
        cfg = r["config"]
        presets = cfg["presets"]
        clear_screen()
        print("=== Пресеты ===\n")
        if presets:
            for i, p in enumerate(presets, 1):
                mark = "*" if cfg["mode"] == "preset" and cfg["preset"] == p["name"] else " "
                print(f" {mark}{i}. {p['name']}  ({p['artist']} - {p['title']}, {fmt_duration(p['duration'])})")
        else:
            print("  (пусто)")
        print("\n  a. Добавить   e. Изменить   d. Удалить   0. Назад")
        choice = input("> ").strip().lower()

        if choice == "0":
            return
        if choice == "a":
            new = form_preset()
            if new is None:
                input("Enter...")
                continue
            if any(p["name"] == new["name"] for p in presets):
                print("Пресет с таким названием уже есть.")
                input("Enter...")
                continue
            request("set", config={"presets": presets + [new]})
        elif choice == "e":
            target = pick_preset(presets, "Какой изменить (номер, Enter: отмена)")
            if target is None:
                continue
            new = form_preset(target)
            if new is None:
                input("Enter...")
                continue
            if new["name"] != target["name"] and any(p["name"] == new["name"] for p in presets):
                print("Пресет с таким названием уже есть.")
                input("Enter...")
                continue
            updated = [new if p["name"] == target["name"] else p for p in presets]
            config = {"presets": updated}
            if cfg["preset"] == target["name"]:
                config["preset"] = new["name"]
            request("set", config=config)
        elif choice == "d":
            target = pick_preset(presets, "Какой удалить (номер, Enter: отмена)")
            if target is None:
                continue
            config = {"presets": [p for p in presets if p["name"] != target["name"]]}
            if cfg["mode"] == "preset" and cfg["preset"] == target["name"]:
                config["mode"] = "off"
            request("set", config=config)


def client():
    if not ensure_daemon():
        print("Не удалось запустить фоновый процесс.")
        print(f"Загляни в лог: {LOG_PATH}")
        input("Enter...")
        return

    # если скрипт переместили, обновляем путь в автозапуске
    if autostart_enabled():
        try:
            set_autostart(True)
        except OSError:
            pass

    note = ""
    while True:
        r = request("get")
        if not r:
            print("Фоновый процесс не отвечает.")
            input("Enter...")
            return
        cfg = r["config"]

        clear_screen()
        print("=== Discord Music Status ===\n")
        print(f"  Фон:         работает, Discord: {'подключён' if r['connected'] else 'не подключён'}")
        print(f"  Режим:       {mode_label(cfg)}")
        print(f"  В статусе:   {r['info']}")
        print(f"  Автозапуск:  {'включён' if autostart_enabled() else 'выключен'}")
        print()
        print("  1. Авто (Яндекс Музыка)")
        print("  2. Выбрать пресет")
        print("  3. Выключить статус")
        print("  4. Пресеты: добавить / изменить / удалить")
        print("  5. Автозапуск с Windows: вкл/выкл")
        print("  6. Полностью остановить программу")
        print("  0. Выйти (программа продолжит работать в фоне)")
        if note:
            print(f"\n  {note}")
            note = ""

        choice = input("\n> ").strip()

        if choice == "1":
            request("set", config={"mode": "auto"})
            time.sleep(1.2)
        elif choice == "2":
            print()
            p = pick_preset(cfg["presets"])
            if p:
                request("set", config={"mode": "preset", "preset": p["name"]})
                time.sleep(1.5)
        elif choice == "3":
            request("set", config={"mode": "off"})
            time.sleep(0.5)
        elif choice == "4":
            manage_presets()
        elif choice == "5":
            try:
                set_autostart(not autostart_enabled())
            except OSError as e:
                note = f"Не получилось изменить автозапуск: {e}"
        elif choice == "6":
            if input("Остановить фоновый процесс и убрать статус? (y/N): ").strip().lower() == "y":
                request("stop")
                print("Остановлено. Следующий запуск скрипта поднимет всё заново.")
                time.sleep(1.5)
                return
        elif choice == "0":
            print("Выхожу. Статус продолжит работать в фоне.")
            return


def main():
    if "--daemon" in sys.argv:
        run_daemon()
        return
    try:
        client()
    except (KeyboardInterrupt, EOFError):
        print("\nВыхожу. Статус продолжит работать в фоне.")


if __name__ == "__main__":
    main()