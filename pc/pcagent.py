"""Скат: исполнитель команд на ПК (Windows). Принимает команды колонки по домашней сети и выполняет ТОЛЬКО то, что разрешено
в config.json: запуск и закрытие программ из белого списка, сайты из белого списка, поиск, медиаклавиши, блокировка экрана.
Никаких произвольных команд, удаления файлов, выключения ПК. Запросы принимаются только с адресов из allow_ip и с токеном.

POST /do  {"action": "open_app|close_app|open_url|search|media|lock", "target": "..."}   заголовок X-Token
GET  /ping
Конфиг: config.json рядом (token, port, allow_ip, apps, sites). Лог: agent.log (последние действия)."""
import collections, ctypes, difflib, io, json, os, re, subprocess, sys, threading, time, urllib.parse
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, "config.json"), encoding="utf-8"))
NOWIN = 0x08000000  # CREATE_NO_WINDOW
CFG["apps"].setdefault("settings", {"launch": ["explorer.exe", "ms-settings:"], "exe": "SystemSettings.exe"})
KEYS = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}


ctypes.windll.user32.SetProcessDPIAware()  # реальные пиксели, а не масштабированные


class MonInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def monitors():
    """{номер: прямоугольник} с номерами как в «Параметры дисплея → Определить» (\\\\.\\DISPLAYn); ключ 0 — основной монитор."""
    found = {}
    Proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_double)

    def cb(h, dc, r, d):
        mi = MonInfo(); mi.cbSize = ctypes.sizeof(MonInfo)
        ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(h), ctypes.byref(mi))
        rect = (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom)
        digits = "".join(c for c in mi.szDevice if c.isdigit())
        found[int(digits) if digits else len(found) + 1] = rect
        if mi.dwFlags & 1: found[0] = rect  # MONITORINFOF_PRIMARY
        return 1
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, Proc(cb), 0)
    return found


def screenshot(n):
    from PIL import ImageGrab
    mons = monitors(); n = int(n)
    box = mons.get(n) or mons.get(0) or next(iter(mons.values()))  # неизвестный номер — основной
    img = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
    if img.width > 1600: img = img.resize((1600, int(img.height * 1600 / img.width)))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=72); return buf.getvalue()


PS_SCAN = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sh = New-Object -ComObject WScript.Shell; $o = @()
foreach ($r in @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  Get-ChildItem $r -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
    $l = $sh.CreateShortcut($_.FullName); $o += [pscustomobject]@{ n = $_.BaseName; t = $l.TargetPath; a = $l.Arguments; l = $_.FullName } } }
$u = Get-StartApps | ForEach-Object { [pscustomobject]@{ n = $_.Name; id = $_.AppID } }
[pscustomobject]@{ lnk = $o; uwp = $u } | ConvertTo-Json -Depth 4 -Compress
"""
# системные оболочки и инструменты, через которые можно выполнить произвольное, в список не попадают никогда
BLOCK = re.compile(r"powershell|pwsh|cmd\.exe|command prompt|командная строка|windows terminal|терминал|regedit|реестр|mmc\.exe|"
                   r"mshta|wscript|cscript|msiexec|uninstall|деинсталл|удалить|удаление|установщик|installer|bash|wsl|ubuntu", re.I)
INDEX = {"apps": [], "t": 0.0}
ALIAS = {"хром": "chrome", "гугл хром": "chrome", "браузер": "chrome", "телеграм": "telegram", "телеграмм": "telegram", "дискорд": "discord",
         "стим": "steam", "ворд": "word", "эксель": "excel", "пауэрпоинт": "powerpoint", "аутлук": "outlook", "оутлук": "outlook",
         "фотошоп": "photoshop", "зум": "zoom", "скайп": "skype", "спотифай": "spotify", "обс": "obs", "пейнт": "paint",
         "блокнот": "notepad", "калькулятор": "calc", "проводник": "explorer", "настройки": "settings", "параметры": "settings",
         "диспетчер задач": "task manager", "яндекс музыка": "yamusic", "яндекс музыку": "yamusic",
         "вс код": "visual studio code", "курсор": "cursor", "эдж": "edge", "эйдж": "edge", "клод": "claude", "гитхаб": "github", "яндекс диск": "yandex disk",
         "эпик геймс": "epic games", "ватсап": "whatsapp", "вайбер": "viber", "олл": "all", "нод": "node"}
TR = dict(zip("абвгдежзийклмнопрстуфхцчшщъыьэюя", ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
          "f", "kh", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def norm(s):
    return re.sub(r"[^0-9a-zа-я ]+", " ", s.lower().replace("ё", "е")).strip()


def translit(s):
    return "".join(TR.get(c, c) for c in s)


def skel(s):
    """Скелет согласных: «киберпанк» и «cyberpunk» дают один и тот же kbrpnk (для русских названий английских программ)."""
    s = s.lower().replace("ph", "f").replace("kh", "h").replace("ck", "k").replace("sch", "sh").replace("ch", "h").replace("x", "ks")
    s = s.translate(str.maketrans({"c": "k", "q": "k", "y": "i", "w": "v", "j": "z"}))
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiou ]", "", s))


def scan():
    """Каталог установленных программ из меню «Пуск»: ярлыки (.lnk) и магазинные приложения (AppID)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_SCAN], capture_output=True, timeout=120, creationflags=0x08000000).stdout
        d = json.loads(out.decode("utf-8", "replace") or "{}")
    except Exception as e:
        log(f"скан программ не удался: {e}"); return
    apps = []
    for l in (d.get("lnk") or []) if isinstance(d.get("lnk"), list) else [d.get("lnk")] if d.get("lnk") else []:
        name, target = l["n"], l.get("t") or ""
        if BLOCK.search(name) or BLOCK.search(os.path.basename(target)): continue
        exe = os.path.basename(target) if target.lower().endswith(".exe") else None
        m = re.search(r"--processStart\s+(\S+)", l.get("a") or "")
        if m: exe = m.group(1)
        apps.append({"name": name, "lnk": l["l"], "exe": exe})
    for u in (d.get("uwp") or []) if isinstance(d.get("uwp"), list) else [d.get("uwp")] if d.get("uwp") else []:
        if BLOCK.search(u["n"]): continue
        apps.append({"name": u["n"], "uwp": u["id"], "exe": None})
    seen, uniq = set(), []
    for a in apps:
        k = norm(a["name"])
        if k and k not in seen: seen.add(k); uniq.append(a)
    INDEX["apps"], INDEX["t"] = uniq, time.time()
    log(f"каталог программ: {len(uniq)} шт.")


def resolve(target):
    """Название, как сказали голосом («дискорд», «обс», «ворд»), -> запись программы или None."""
    t = norm(target)
    if t in CFG["apps"]: return {"name": t, "manual": CFG["apps"][t]}
    t = ALIAS.get(t, t)
    if t in CFG["apps"]: return {"name": t, "manual": CFG["apps"][t]}
    best, who = 0.0, None
    for a in INDEX["apps"]:
        n = norm(a["name"]); tn = translit(t)
        score = max(difflib.SequenceMatcher(None, t, n).ratio(), difflib.SequenceMatcher(None, tn, n).ratio())
        toks = n.split()
        if t == n or (len(t) >= 3 and (t in toks or tn in toks)): score = max(score, 0.97)
        elif len(t) >= 3 and any(k.startswith(t) or k.startswith(tn) for k in toks): score = max(score, 0.9)  # начало слова, не середина
        sk = skel(tn)
        if len(sk) >= 3 and (sk == skel(n) or sk in {skel(k) for k in toks if len(k) >= 3}): score = max(score, 0.92)
        if score > best: best, who = score, a
    return who if best >= 0.8 else None


def launch(a):
    if "manual" in a: subprocess.Popen(a["manual"]["launch"])
    elif a.get("uwp"): subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + a["uwp"]])
    else: os.startfile(a["lnk"])


def close(a):
    exe = (a.get("manual") or a).get("exe")
    if exe: subprocess.run(["taskkill", "/IM", exe], creationflags=0x08000000, capture_output=True)  # без /F: вежливо
    else:
        subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process | Where-Object { $_.MainWindowTitle -like $env:SKAT_T } | ForEach-Object { [void]$_.CloseMainWindow() }"],
                       creationflags=0x08000000, capture_output=True, env={**os.environ, "SKAT_T": "*" + a["name"] + "*"})


def log(msg):
    try:
        p = os.path.join(DIR, "agent.log"); old = open(p, encoding="utf-8").read().splitlines()[-200:] if os.path.exists(p) else []
        open(p, "w", encoding="utf-8").write("\n".join(old + [f"{time.strftime('%d.%m %H:%M:%S')} {msg}"]) + "\n")
    except OSError: pass


def key(code, n=1):
    for _ in range(n):
        ctypes.windll.user32.keybd_event(code, 0, 0, 0); ctypes.windll.user32.keybd_event(code, 0, 2, 0)


def browser(url):
    chrome = CFG["apps"].get("chrome", {}).get("launch")
    subprocess.Popen(chrome + [url] if chrome else ["explorer.exe", url])


def do(action, target):
    target = (target or "").strip()
    if action in ("open_app", "close_app"):
        app = resolve(target)
        if not app and time.time() - INDEX["t"] > 60: scan(); app = resolve(target)  # возможно, программу установили недавно
        if not app: return False, f"не нашла программу «{target}»"
        launch(app) if action == "open_app" else close(app)
        log(f"{action}: «{target}» -> {app['name']}")
    elif action == "open_url":
        u = urllib.parse.urlparse(target if "//" in target else "https://" + target)
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not any(host == s or host.endswith("." + s) for s in CFG["sites"]):
            return False, "этот сайт не в списке разрешённых"
        browser(u.geturl())
    elif action == "search":
        if not target or len(target) > 200: return False, "пустой запрос"
        browser("https://yandex.ru/search/?text=" + urllib.parse.quote(target))
    elif action == "media":
        if target not in KEYS: return False, "неизвестная медиакоманда"
        key(KEYS[target], 5 if target in ("volup", "voldown") else 1)
    elif action == "lock":
        ctypes.windll.user32.LockWorkStation()
    else:
        return False, "неизвестное действие"
    return True, ""


AUD = {"clients": [], "thread": None}


def audio_capture():
    """Захват того, что играет на ПК (loopback). Пока есть слушатели, нарезаем на блоки по 40 мс и раздаём всем."""
    import numpy as np, pyaudiowpatch as pa
    p = pa.PyAudio()
    devs = list(p.get_loopback_device_info_generator()); want = CFG.get("audio_device", "").lower()
    dev = next((d for d in devs if want and want in d["name"].lower()), None) or p.get_default_wasapi_loopback()  # звук того устройства, куда Windows играет сейчас
    rate, ch = int(dev["defaultSampleRate"]), dev["maxInputChannels"]
    log(f"звук ПК: {dev['name']} {rate} Гц")
    state = {"buf": np.zeros(0, np.float32), "pos": 0.0}

    def cb(data, frames, t, status):
        x = np.frombuffer(data, np.int16).reshape(-1, ch).mean(1)
        if rate != 48000:  # линейная передискретизация с непрерывной фазой между блоками
            idx = np.arange(state["pos"], len(x) - 1, rate / 48000); state["pos"] = idx[-1] + rate / 48000 - len(x) if len(idx) else 0.0
            x = np.interp(idx, np.arange(len(x)), x)
        b = np.concatenate((state["buf"], x))
        while len(b) >= 1920:
            chunk, b = np.clip(b[:1920], -32768, 32767).astype(np.int16).tobytes(), b[1920:]
            for q in list(AUD["clients"]): q.append(chunk)
        state["buf"] = b
        return (None, pa.paContinue)
    st = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True, input_device_index=dev["index"], frames_per_buffer=rate // 50, stream_callback=cb)  # 20 мс: задержка захвата минимальная
    try:
        while AUD["clients"]: time.sleep(0.2)
    finally:
        st.close(); p.terminate(); AUD["thread"] = None


SKY_EXE = r"C:\Program Files (x86)\Skydimo\SkyDimo.exe"
SKY = {"ser": None, "cur": [0.0, 0.0, 0.0], "tgt": (0, 0, 0), "base": (130, 60, 230), "on": True, "gain": 1.0, "n": 65}
SKY_LOCK = threading.Lock()


def sky_set(rgb):
    SKY["tgt"] = tuple(int(c * SKY["gain"]) for c in rgb) if SKY["on"] else (0, 0, 0)


def sky_loop(ser):
    """Кадры как у приложения: «Ada» 00 00 N + RGB (гамма 2.2), ~22 раза в секунду, цвет плавно догоняет цель."""
    while SKY["ser"] is ser:
        try:
            SKY["cur"] = [c + (t - c) * 0.55 for c, t in zip(SKY["cur"], SKY["tgt"])]
            px = bytes(round(255 * (min(255, max(0, c)) / 255) ** 2.2) for c in SKY["cur"])
            ser.write(b"Ada\x00\x00" + bytes([SKY["n"]]) + px * SKY["n"])
        except Exception as e:
            log(f"skydimo порт: {e}"); sky_release(); return
        time.sleep(0.045)


def sky_take():
    """Skydimo держит COM-порт один: закрываем его приложение, здороваемся с лентой («Moni-A») и ведём её сами."""
    with SKY_LOCK:
        if SKY["ser"]: return True
        import serial
        subprocess.run(["taskkill", "/F", "/IM", "SkyDimo.exe"], capture_output=True); time.sleep(1.5)
        ser = serial.Serial(CFG.get("sky_port", "COM9"), 115200, timeout=0.5, write_timeout=2); ser.dtr = ser.rts = True
        time.sleep(1.2); ser.write(b"Moni-A"); time.sleep(0.5); ser.reset_input_buffer()
        SKY["cur"] = [0.0, 0.0, 0.0]; SKY["on"] = True; sky_set(SKY["base"]); SKY["ser"] = ser
        threading.Thread(target=sky_loop, args=(ser,), daemon=True).start()
        log("лента монитора: ведёт Скат"); return True


def sky_release():
    with SKY_LOCK:
        ser, SKY["ser"] = SKY["ser"], None
        if ser:
            try: ser.close()
            except Exception: pass
            subprocess.Popen([SKY_EXE, "--auto_startup"]); log("лента монитора: вернул Skydimo")


def sky_cmd(p):
    """/sky/<действие>[/R/G/B | /N]; временные цвета (rgb) и постоянный (base), как у ленты в комнате."""
    a = p[0]
    if a == "take": sky_take(); return {"ok": True, "owned": True}
    if a == "release": sky_release(); return {"ok": True, "owned": False}
    if not SKY["ser"]: return {"ok": False, "owned": False}
    v = [max(0, min(255, int(x))) for x in p[1:4]] if a in ("rgb", "base") else None
    if a == "rgb": sky_set(v)
    elif a == "base": SKY["base"] = tuple(v); SKY["on"] = True; sky_set(v)
    elif a == "restore": sky_set(SKY["base"])
    elif a == "off": SKY["on"] = False; sky_set((0, 0, 0))
    elif a == "on": SKY["on"] = True; sky_set(SKY["base"])
    elif a == "gain": SKY["gain"] = max(0.03, min(1.0, int(p[1]) / 100)); sky_set(SKY["base"])
    return {"ok": True, "owned": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def allowed(self):
        ip, nets = self.client_address[0], {a.rsplit(".", 1)[0] for a in CFG["allow_ip"] if a != "127.0.0.1"}
        # колонка может сменить адрес после перезагрузки роутера: пускаем всю домашнюю подсеть, но только с токеном
        return (ip in CFG["allow_ip"] or ip.rsplit(".", 1)[0] in nets) and self.headers.get("X-Token") == CFG["token"]

    def do_GET(self):
        if self.path == "/audio" and self.allowed(): return self.audio()
        self.reply(200, {"ok": True}) if self.path == "/ping" and self.allowed() else self.reply(403, {"ok": False})

    def audio(self):
        """Колонка читает звук ПК: PCM S16 48 кГц моно, блоки по 3840 байт. Медленному слушателю старое отбрасываем: звук всегда «живой»."""
        q = collections.deque(maxlen=3); AUD["clients"].append(q)  # 0,5 с: дальше старое выбрасываем, задержка не копится
        try: self.connection.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_SNDBUF, 4096)
        except OSError: pass
        if not (AUD["thread"] and AUD["thread"].is_alive()):
            AUD["thread"] = threading.Thread(target=audio_capture, daemon=True); AUD["thread"].start()
        log("звук ПК: колонка подключилась")
        try:
            self.send_response(200); self.send_header("Content-Type", "audio/L16"); self.send_header("Connection", "close"); self.end_headers()
            while True:
                t0 = time.time()
                while not q and time.time() - t0 < 0.25: time.sleep(0.004)
                blk = q.popleft() if q else bytes(3840)  # на ПК тишина: шлём тишину, чтобы поток не замирал
                self.wfile.write(__import__("struct").pack("<d", time.time()) + blk if self.headers.get("X-Ts") else blk)  # X-Ts: отладка — метка времени отправки
                self.wfile.flush()
        except (OSError, ValueError): pass
        finally:
            AUD["clients"].remove(q); log("звук ПК: колонка отключилась")

    def do_POST(self):
        if self.path.startswith("/sky/") and self.allowed():
            try: return self.reply(200, sky_cmd(self.path.strip("/").split("/")[1:]))
            except Exception as e: log(f"sky error {e}"); return self.reply(500, {"ok": False, "error": str(e)[:80]})
        if self.path == "/show" and self.allowed():  # нарисованная картинка -> открыть в просмотрщике Windows
            n = min(int(self.headers.get("Content-Length", 0)), 12_000_000); b = self.rfile.read(n)
            if not (b[:8] == b"\x89PNG\r\n\x1a\n" or b[:3] == b"\xff\xd8\xff"): return self.reply(400, {"ok": False, "error": "не картинка"})
            f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skat_art.png" if b[:4] == b"\x89PNG" else "skat_art.jpg")
            open(f, "wb").write(b); os.startfile(f); log(f"картинка показана ({len(b) // 1024} КБ)")
            return self.reply(200, {"ok": True})
        if self.path == "/screenshot" and self.allowed():  # снимок монитора для «посмотри и подскажи»
            try:
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1024)) or b"{}")
                jpg = screenshot(d.get("monitor", 1))
            except Exception as e:
                log(f"screenshot error {e}"); return self.reply(500, {"ok": False, "error": str(e)[:80]})
            log(f"screenshot монитор {d.get('monitor')} {len(jpg)//1024} КБ")
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(jpg)))
            self.end_headers(); self.wfile.write(jpg); return
        if self.path != "/do" or not self.allowed(): return self.reply(403, {"ok": False})
        try: d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)) or b"{}")
        except ValueError: return self.reply(400, {"ok": False})
        try: ok, err = do(d.get("action", ""), d.get("target", ""))
        except Exception as e: ok, err = False, str(e)[:80]
        log(f"{d.get('action')} {d.get('target')!r} -> {'ok' if ok else err}")
        self.reply(200, {"ok": ok, "error": err})


if __name__ == "__main__":
    log("агент запущен")
    threading.Thread(target=lambda: [scan() or time.sleep(900) for _ in iter(int, 1)], daemon=True).start()  # каталог обновляем раз в 15 минут
    ThreadingHTTPServer(("0.0.0.0", CFG["port"]), H).serve_forever()
