#!/usr/bin/env python3
"""Клиент колонки. Слово «Скат» ловит локальный детектор (wake.py, без облака): сразу подсветка, потом команда
режется энергетическим VAD, уходит на бэкенд (STT -> DeepSeek -> TTS) и ответ стримом играет в динамик.
Кнопки: громкость +/-, микрофон (mute), голосовая/prog (слушать без «Скат» / прервать ответ).
Подсветка, циферблат и звуки: заводские ресурсы Маруси (ring.py, clock.py, /opt/skat/led, /opt/skat/snd).
Диалог: если ответ закончился вопросом, колонка ещё LISTEN_SEC слушает без «Скат».
Конфиг: /etc/skat.conf (URL=, TOKEN=, CERT=, THRESH=, DEVICE=, BRIGHT=, CLOCK_BRIGHT=, MIC_GAIN=, WAKE_THR=, DEBUG=)."""
import array, collections, colorsys, http.client, json, math, os, queue, re, ssl, struct, subprocess, threading, time, urllib.parse
import numpy as np
import aec as aecmod, clock as clockmod, music as musicmod, ring as ringmod, wake as wakemod

CONF = dict(l.strip().split("=", 1) for l in open("/etc/skat.conf") if "=" in l and not l.startswith("#"))
URL = urllib.parse.urlparse(CONF["URL"])
CTX = ssl.create_default_context(cafile=CONF["CERT"])
HDR = {"X-Token": CONF["TOKEN"], "X-Device": CONF.get("DEVICE", "kolonka")}
THRESH = float(CONF.get("THRESH", 600))  # минимальный RMS речи; реально порог = max(THRESH, 3 * шум комнаты)
CMD_THR = float(CONF.get("CMD_THR", 0.97))  # порог командных слов (дальше/назад/пауза) при музыке, без «Скат»
_mu = urllib.parse.urlparse(CONF.get("MUSIC_URL", ""))
LED_URL = urllib.parse.urlparse(f"http://{_mu.hostname}:8781") if _mu.hostname and CONF.get("LED", "1") == "1" else None
WAKE_THR = float(CONF.get("WAKE_THR", 0.93))  # порог вероятности слова «Скат»
LIGHT_DELAY = float(CONF.get("LIGHT_DELAY", 0.1))  # задержка запуска звука aplay относительно записи, с; подстройка света
FRAME = 320  # 20 мс, 16 кГц
START, END, PRE, MAXF = 4, 20, 15, 500  # кадров: старт речи, тишина до конца (0,4 с), пред-запись (0,3 с), максимум 10 с
# Все 4 PDM-микрофона 48 кГц усредняем и приводим к 16 кГц нормальным ресемплером sox (дешёвый plug из ALSA
# даёт алиасинг и берёт один микрофон)
REC = ["sh", "-c", "arecord -q -D micboost -f S32_LE -c 4 -r 48000 -t raw | "
       "sox -t raw -r 48000 -e signed -b 32 -c 4 - -t raw -r 16000 -e signed -b 16 -c 1 - channels 1 rate -h 2>/dev/null"]
PLAY = ["aplay", "-q", "-D", "default", "-t", "raw", "-f", "S16_LE", "-r", "24000", "-c", "1"]
PLAY_MUSIC = PLAY[:-3] + ["48000", "-c", "1"]  # музыка и звук ПК идут в 48 кГц, голос ассистента — в 24 кГц
INPUT, SND, LISTEN_SEC = "/dev/input/event0", "/opt/skat/snd", 10
MUTE_FILE = "/var/lib/skat-muted"

muted = os.path.exists(MUTE_FILE)  # выключенный микрофон остаётся выключенным после перезапуска
vol_idx = 8
awaiting_until = awaiting_since = chime_at = 0.0  # слушаем команду: кольцо показывает уровень голоса
player = None
busy = threading.Event()  # идёт запрос/ответ: микрофон не слушаем
deaf_until = 0.0
ring = ringmod.Ring(float(CONF.get("BRIGHT", 1.0)))
ring.theme = (122, 60, 255)
clock = clockmod.Clock(float(CONF.get("CLOCK_BRIGHT", 1.0)))
wake = wakemod.Wake(marusia=CONF.get("WAKEWORD") == "marusia")
try: live = aecmod.Live(int(CONF.get("AEC_FILTER", 4096)))  # эхоподавление Speex для слова «Скат» поверх музыки
except OSError as e: live = None; print("AEC off:", e, flush=True)
viz_q = queue.Queue(maxsize=200)
viz_block_until = 0.0  # до этого момента кольцо занято (показ громкости)
music = None
music_paused = False  # пользователь сам попросил паузу: после ответа не возобновляем


def rms(b):
    a = array.array("h", b)
    return (sum(x * x for x in a) / len(a)) ** 0.5


def sound(name, wait=False):
    global deaf_until  # свой звук не должен услышать микрофон
    margin = 0.12 if name == "03_wake_short" else 0.6  # хвост эха; после короткого сигнала слушаем почти сразу
    if not os.path.exists(f"{SND}/{name}.wav"): return  # заводских звуков нет — молчим
    deaf_until = max(deaf_until, time.time() + (os.path.getsize(f"{SND}/{name}.wav") - 44) / 96000 + margin)
    p = subprocess.Popen(["aplay", "-q", "-D", "default", f"{SND}/{name}.wav"], stderr=subprocess.DEVNULL)
    if wait: p.wait()


def ui_idle():
    """Покой: кольцо выключено (при выключенном микрофоне красный индикатор), на циферблате время."""
    if muted: ring.show("mic_off", 10**6)
    else: ring.off()
    clock.time()
    if music and not music_paused: music.resume()  # ответ закончился — музыка продолжается
    if not (music and music.playing): ledc.restore()


THEME = (122, 60, 255)  # «Королевская»: фиолетовый, к громкому голосу переходит в золото
BLUE = THEME  # прежнее имя: цвет «слушаю»
GRAD = [(0.0, THEME), (0.3, (154, 85, 240)), (0.55, (197, 138, 90)), (0.78, (240, 183, 46)), (1.0, (255, 216, 102))]


def level_color(t):
    """Громкость голоса 0..1 -> цвет градиента синий-голубой-зелёный-жёлтый-красный."""
    t = max(0.0, min(1.0, t))
    for (a, ca), (b, cb) in zip(GRAD, GRAD[1:]):
        if t <= b:
            k = (t - a) / (b - a); return tuple(int(x + (y - x) * k) for x, y in zip(ca, cb))
    return GRAD[-1][1]


def start_listening():
    global awaiting_until, awaiting_since
    awaiting_since = time.time(); awaiting_until = time.time() + LISTEN_SEC


def ui_listen(chime):
    """Колонка слушает команду: кольцо синее, открытые глаза, дальше цвет следит за голосом."""
    ring.color(BLUE); clock.play("eyesReady_digits", hold=True); ledc.rgb([c * 0.5 for c in THEME])  # лента загорается вместе с кольцом
    start_listening()
    if chime: sound(chime, wait=True)


def call(path, body=b""):
    c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30)
    c.request("POST", path, body, HDR)
    return c.getresponse()


def stop_playback():
    if player and player.poll() is None:
        player.kill()


warm = None  # (соединение, время): TLS-рукопожатие делаем заранее, пока человек говорит команду


def warm_up():
    global warm
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30); c.connect(); warm = (c, time.time())
    except OSError: warm = None


TINTS = {"joy": (255, 190, 0), "laugh": (255, 110, 0), "anger": (255, 15, 0), "surprise": (0, 230, 255),
         "irony": (170, 0, 255), "sad": (0, 40, 255), "tired": (0, 120, 110)}  # оттенок свечения голоса; calm — обычный градиент


def send_and_play(pcm, awake, t_end):
    global player, awaiting_until, warm
    w, warm = warm, None
    c = w[0] if w and time.time() - w[1] < 15 else http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30)
    # «Скат» уже поймал детектор; X-Music: колонка сама сообщает, что музыка играет (сервер мог перезапуститься)
    c.request("POST", "/ask", pcm, {**HDR, "Content-Type": "audio/L16", "X-Force": "1",
                                    **({"X-Music": "1"} if music and music.playing else {})})
    awaiting_until = 0
    if awake: ring.play("thinking_loop", loop=True)  # «думаю» сразу: заголовок ответа приходит вместе с первым звуком
    else: ring.play("listening_start", then=("thinking_loop", True))
    clock.play("eyesBlink2_digits", loop=True)
    r = c.getresponse()
    if r.status != 200:
        r.read(); return r.status
    if r.getheader("X-Kind") == "ack":  # в фразе осталось только «Скат» (повтор): продолжаем слушать
        r.read(); ring.color(BLUE); clock.play("eyesReady_digits", hold=True); start_listening(); return 200
    emo = r.getheader("X-Emo", "calm"); tint = TINTS.get(emo)
    lh = r.getheader("X-Light")  # команда ленте: применяем сразу и не мигаем цветами эмоции поверх неё
    if lh:
        try: do_light(json.loads(lh))
        except ValueError: pass
    player = p = subprocess.Popen(PLAY, stdin=subprocess.PIPE)
    lights, stop_lights, t0, played = queue.Queue(), threading.Event(), None, 0

    def light_loop():  # свет по времени ВОСПРОИЗВЕДЕНИЯ, а не записи в aplay (там буфер на секунды)
        while (item := lights.get()) is not None and not stop_lights.is_set():
            time.sleep(max(0, item[0] - time.time()))
            if not stop_lights.is_set():
                ring.show("speaking", item[1], tint)
                if tint and not lh: ledc.rgb([x * (0.35 + 0.65 * min(1.0, item[1] / 10)) for x in tint])  # лента светится только на эмоциях
    threading.Thread(target=light_loop, daemon=True).start()
    done, gap = threading.Event(), {"shown": False}

    def gap_watch():  # звук доиграл, а продолжения нет: сервер что-то ищет в интернете
        while not done.wait(0.15):
            if t0 is not None and not gap["shown"] and time.time() > t0 + played / 48000 + 0.35:
                gap["shown"] = True; loading_ui()
    threading.Thread(target=gap_watch, daemon=True).start()
    try:
        while d := r.read(8192):
            if gap["shown"]: gap["shown"] = False; clock.face(emo); t0 = time.time() + LIGHT_DELAY; played = 0
            if t0 is None:
                clock.face(emo); t0 = time.time() + LIGHT_DELAY
                print(f"latency: {time.time()-t_end:.2f}s from end of speech to first audio", flush=True)
            lights.put((t0 + played / 48000, int(rms(d[:len(d) // 2 * 2]) / 70)))  # 24 кГц * 2 байта
            played += len(d)
            p.stdin.write(d)
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # прервали кнопкой
    p.wait(); stop_lights.set(); lights.put(None); done.set()
    fu = {}
    if player.returncode == 0:
        try: fu = json.loads(call("/followup").read() or b"{}")
        except (OSError, ValueError) as e: print("followup error", e, flush=True)
        print("followup:", fu, flush=True)
    else: print("followup skipped, aplay exit", player.returncode, flush=True)
    if fu.get("light"): do_light(fu["light"])
    if fu.get("image"): threading.Thread(target=show_image, args=(fu["image"],), daemon=True).start()
    started = fu.get("music") and do_music(fu["music"])
    if fu.get("q"):  # ответ закончился вопросом
        ui_listen("03_wake_short")
    elif not started:  # при запуске музыки кольцо и циферблат ведёт загрузка, потом бит
        ui_idle()
    return 200


LOADER, DONE = "downloadIconLoop_digits", "updateDownloadСomplete_diodes"  # иконка загрузки и «готово» из заводских ресурсов


def loading_ui():
    clock.spin(); ring.play("loading", loop=True)


LIVE = {"on": False, "sid": None, "q": queue.Queue(maxsize=600), "mute_until": 0.0, "play_end": 0.0, "stop": threading.Event(), "player": None, "ended": False}


VG = {"buf": b"", "g": 0.0, "sp": 1.0}


def vg_update(f):
    """Ворота «это голос»: нейросеть-различитель речи и шума (vad_model.npz) крутит те же признаки, что и детектор слова. 1.0 — речь, 0 — шум."""
    if wake.vad is None: return 1.0
    VG["buf"] += f
    while len(VG["buf"]) >= 2560:
        wake.feed(np.frombuffer(VG["buf"][:2560], np.int16)); VG["buf"] = VG["buf"][2560:]; VG["sp"] = wake.sp
    VG["g"] = max(min(1.0, max(0.0, (VG["sp"] - 0.3) / 0.45)), VG["g"] * 0.9)  # атака сразу, спад ~0,4 с
    return VG["g"]


def live_feed(f):
    """Кадр микрофона (16 кГц, 20 мс) -> очередь в OpenAI (24 кГц). Пока Скат говорит, шлём тишину: иначе он услышит себя."""
    x = np.frombuffer(f, np.int16)
    if time.time() < LIVE["mute_until"]: pcm = bytes(int(len(x) * 1.5) * 2)
    else: pcm = np.interp(np.arange(int(len(x) * 1.5)) / 1.5, np.arange(len(x)), x).astype(np.int16).tobytes()
    try: LIVE["q"].put_nowait(pcm)
    except queue.Full: pass
    LIVE["n"] = LIVE.get("n", 0) + 1
    if LIVE["n"] % 3 == 0 and time.time() > LIVE["mute_until"]:  # кольцо как индикатор уровня твоего голоса
        lv = max(0.0, min(1.0, (rms(f) - 250) / 1800)) ** 0.7 * vg_update(f); col = level_color(lv); dim = tuple(int(c * 0.10) for c in THEME)
        ring.colors([tuple(int(c * a) for c in col) if (a := min(1.0, lv * 7.5 + 0.5 - abs(i - 6))) > 0 else dim for i in range(12)])
        ledc.rgb([c * (0.3 + 0.7 * lv) for c in col])


def live_start():
    """Живой разговор: микрофон идёт в OpenAI Realtime через наш сервер, голос возвращается и играет сразу."""
    if LIVE["on"]: return
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=20); c.request("POST", "/live/start", b"", HDR)
        r = c.getresponse(); sid = json.loads(r.read())["sid"]
    except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
        print("live: не удалось начать", repr(e)[:80], flush=True); speak("Живой разговор не включился."); return
    while not LIVE["q"].empty(): LIVE["q"].get_nowait()
    LIVE.update(on=True, sid=sid, mute_until=0.0, play_end=0.0, n=0, ended=False); LIVE["stop"].clear()
    if music and music.playing: threading.Thread(target=music.suspend, daemon=True).start()
    ring.color(THEME); clock.face("calm"); ledc.rgb((255, 190, 120))
    print(f"{time.strftime('%H:%M:%S')} живой разговор начат", flush=True)
    threading.Thread(target=live_up, args=(sid,), daemon=True).start()
    threading.Thread(target=live_down, args=(sid,), daemon=True).start()


def live_up(sid):
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30)
        c.putrequest("POST", f"/live/up?sid={sid}")
        for k, v in HDR.items(): c.putheader(k, v)
        c.putheader("Transfer-Encoding", "chunked"); c.endheaders()
        while LIVE["on"] and LIVE["sid"] == sid:
            try: b = LIVE["q"].get(timeout=0.5)
            except queue.Empty: continue
            for _ in range(3):  # ~80 мс в одном куске
                try: b += LIVE["q"].get_nowait()
                except queue.Empty: break
            c.send(b"%x\r\n%s\r\n" % (len(b), b))
        c.send(b"0\r\n\r\n")
    except (OSError, http.client.HTTPException) as e: print("live up:", repr(e)[:80], flush=True)
    live_end(sid)


def live_down(sid):
    def readn(r, n):
        d = b""
        while len(d) < n:
            c = r.read(n - len(d))
            if not c: raise OSError("поток закрыт")
            d += c
        return d
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=90); c.request("GET", f"/live/down?sid={sid}", headers=HDR)
        r = c.getresponse()
        while LIVE["on"] and LIVE["sid"] == sid:
            typ, n = struct.unpack(">BI", readn(r, 5)); data = readn(r, n) if n else b""
            if typ == 0:
                p = LIVE["player"]
                if p is None or p.poll() is not None: LIVE["player"] = p = subprocess.Popen(PLAY, stdin=subprocess.PIPE)
                try: p.stdin.write(data); p.stdin.flush()
                except (BrokenPipeError, OSError): LIVE["player"] = None
                now = time.time(); LIVE["play_end"] = max(now, LIVE["play_end"]) + len(data) / 48000; LIVE["mute_until"] = LIVE["play_end"] + 0.5
                ring.show("speaking", min(100, int(rms(data[:len(data) // 2 * 2]) / 70)))
            elif typ == 2:  # человек заговорил поверх: сбрасываем недоигранное
                p = LIVE["player"]; LIVE["player"] = None; LIVE["play_end"] = LIVE["mute_until"] = time.time()
                if p and p.poll() is None: p.kill()
            elif typ == 1: break
    except (OSError, http.client.HTTPException, struct.error) as e: print("live down:", repr(e)[:80], flush=True)
    live_end(sid)


def live_end(sid=None):
    """Конец разговора: договариваем то, что в буфере, возвращаем музыку и покой."""
    if not LIVE["on"] or (sid and LIVE["sid"] != sid): return
    sid = LIVE["sid"]; time.sleep(max(0.0, min(8.0, LIVE["play_end"] - time.time())))
    LIVE["on"] = False; LIVE["ended"] = True
    p = LIVE["player"]; LIVE["player"] = None
    if p:
        try: p.stdin.close(); p.wait(3)
        except (OSError, subprocess.TimeoutExpired): p.kill()
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=5); c.request("POST", f"/live/stop?sid={sid}", b"", HDR); c.getresponse().read()
    except (OSError, http.client.HTTPException): pass
    print(f"{time.strftime('%H:%M:%S')} живой разговор закончен", flush=True)
    ui_idle()


def show_image(iid):
    """Картинка, нарисованная на сервере: забираем и отдаём агенту на ПК — он открывает её на мониторе."""
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30)
        c.request("GET", "/img/" + iid, headers=HDR); r = c.getresponse(); png = r.read()
        if r.status != 200: return print("image: сервер вернул", r.status, flush=True)
        u = urllib.parse.urlparse(CONF["PC_URL"]); p = http.client.HTTPConnection(u.hostname, u.port, timeout=20)
        p.request("POST", "/show", png, {"X-Token": CONF["PC_TOKEN"]}); print("image: показана", p.getresponse().status, flush=True)
    except (OSError, KeyError, http.client.HTTPException) as e: print("image error", e, flush=True)


def do_light(a):
    """Голосовая команда подсветке: включить, выключить, цвет, яркость."""
    act = a.get("action")
    if CONF.get("SKY_ON_PI"):  # лента монитора подключена к Pi и всегда с нами: брать и возвращать нечего
        a = {**a, "monitor": False}
        if act in ("sky_take", "sky_release"): return
    if a.get("monitor") and not SKY_ON[0] and act not in ("sky_take", "sky_release"):  # команда про монитор: сначала берём ленту у Skydimo
        def run():
            try: pc_call("/sky/take", {}, 20); SKY_ON[0] = True
            except (OSError, ValueError): print("sky: нет связи с агентом ПК", flush=True)
            do_light({**a, "monitor": False})
        threading.Thread(target=run, daemon=True).start()  # комнатная лента реагирует сразу, монитор — после захвата
    if act in ("sky_take", "sky_release"):
        def run():
            try: pc_call("/sky/take" if act == "sky_take" else "/sky/release", {}, 20); SKY_ON[0] = act == "sky_take"; ledc.restore()
            except (OSError, ValueError): print("sky: нет связи с агентом ПК", flush=True)
        threading.Thread(target=run, daemon=True).start(); return
    if act == "off": ledc.send("/off")
    elif act == "color" and a.get("rgb"): ledc.send("/base/%d/%d/%d" % tuple(a["rgb"]))
    elif act == "brightness" and a.get("gain"): ledc.send("/gain/%d" % int(a["gain"]))
    else: ledc.send("/on")


def music_started():
    """Первый звук музыки: кольцо показывает «загрузка завершена», дальше его подхватывает бит."""
    global viz_block_until
    try: viz_block_until = time.time() + sum(d for d, _ in ring.anim(DONE)) + 0.1
    except OSError: pass
    ring.play(DONE); clock.time()


class Beat:
    """Бас музыки -> бит: энергия низких частот выше скользящего среднего."""
    def __init__(self): self.hist, self.last, self.peak = collections.deque(maxlen=40), 0.0, 1.0

    def feed(self, block, t):
        x = np.frombuffer(block, np.int16).astype(np.float32)
        e = float(np.sqrt(np.mean(np.convolve(x, np.ones(6) / 6, "same") ** 2)))  # грубый фильтр НЧ
        avg = sum(self.hist) / len(self.hist) if self.hist else e
        self.hist.append(e); self.peak = max(e, self.peak * 0.995)
        beat = e > 1.35 * avg + 150 and t - self.last > 0.28
        if beat: self.last = t
        return min(1.0, e / (self.peak + 1)), beat


beat_detector = Beat()


HOSTS = {"music": ("MUSIC_URL", 8780, "/now", "MUSIC_TOKEN"), "led": (None, 8781, "/state", "MUSIC_TOKEN"), "pc": ("PC_URL", 8790, "/ping", "PC_TOKEN")}
HOSTS_FILE, hosts_kick = "/var/lib/skat-hosts.json", threading.Event()


def probe(ip, port, path, token):
    try:
        c = http.client.HTTPConnection(ip, port, timeout=1.5); c.request("GET", path, headers={"X-Token": token}); ok = c.getresponse().status == 200; c.close()
        return ok
    except (OSError, http.client.HTTPException): return False


def scan_net(port, path, token):
    """Адреса в доме после перезагрузки роутера меняются: ищем сервис по всей подсети (порт открыт и отвечает нашему токену)."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); mine = s.getsockname()[0]; s.close()
    pre = mine.rsplit(".", 1)[0]

    def one(i):
        ip = f"{pre}.{i}"
        if ip == mine: return None
        try: socket.create_connection((ip, port), 0.5).close()
        except OSError: return None
        return ip if probe(ip, port, path, token) else None
    with ThreadPoolExecutor(64) as ex: return next((ip for ip in ex.map(one, range(2, 255)) if ip), None)


def apply_host(name, ip):
    """Подставляем найденный адрес там, где сервис используется."""
    global LED_URL
    key, port = HOSTS[name][0], HOSTS[name][1]
    if name == "music" and music: music.host = ip
    if name == "led": LED_URL = urllib.parse.urlparse(f"http://{ip}:{port}"); ledc.c = None
    if key: CONF[key] = f"http://{ip}:{port}"


def hosts_loop():
    """Раз в минуту (и сразу после сбоя) проверяем Pi и ПК; пропали — ищем заново и запоминаем в /var/lib/skat-hosts.json."""
    try: saved = json.load(open(HOSTS_FILE))
    except (OSError, ValueError): saved = {}
    for name, ip in saved.items(): apply_host(name, ip)
    while True:
        hosts_kick.wait(60); hosts_kick.clear()
        for name, (key, port, path, tok) in HOSTS.items():
            url = LED_URL.geturl() if name == "led" and LED_URL else CONF.get(key or "")
            if not url: continue
            cur = urllib.parse.urlparse(url).hostname
            if cur and probe(cur, port, path, CONF.get(tok, "")): continue
            ip = scan_net(port, path, CONF.get(tok, ""))
            if ip and ip != cur:
                print(f"hosts: {name} {cur} -> {ip}", flush=True); apply_host(name, ip); saved[name] = ip
                try: json.dump(saved, open(HOSTS_FILE, "w"))
                except OSError: pass


SKY_ON = [False]  # ленту монитора сейчас ведёт Скат (иначе Skydimo)


class LedStrip:
    """Лента на Pi (ledd.py): цвета уходят в отдельном потоке, подряд идущие временные цвета склеиваются (побеждает последний)."""

    def __init__(self):
        self.q, self.cv, self.c, self.pc = collections.deque(), threading.Condition(), None, None
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, path, merge=False):
        if not LED_URL: return
        with self.cv:
            if merge and self.q and self.q[-1][1]: self.q[-1] = (path, True)
            else: self.q.append((path, merge))
            self.cv.notify()

    def rgb(self, c): self.send("/rgb/%d/%d/%d" % tuple(max(0, min(255, int(x))) for x in c), True)
    def restore(self): self.send("/restore")

    def _run(self):
        while True:
            with self.cv:
                while not self.q: self.cv.wait()
                path, _ = self.q.popleft()
            try:
                if self.c is None: self.c = http.client.HTTPConnection(LED_URL.hostname, LED_URL.port, timeout=4)
                self.c.request("POST", path, b"", {"X-Token": CONF.get("MUSIC_TOKEN", "")}); self.c.getresponse().read()
            except (OSError, http.client.HTTPException): self.c = None; hosts_kick.set(); time.sleep(1)
            if SKY_ON[0] and CONF.get("PC_URL"):  # лента монитора (Skydimo), пока её ведёт Скат
                try:
                    u = urllib.parse.urlparse(CONF["PC_URL"])
                    if self.pc is None: self.pc = http.client.HTTPConnection(u.hostname, u.port, timeout=4)
                    self.pc.request("POST", "/sky" + path, b"", {"X-Token": CONF["PC_TOKEN"]})
                    if not json.loads(self.pc.getresponse().read() or b"{}").get("owned"): SKY_ON[0] = False  # агент перезапустили
                except (OSError, ValueError, http.client.HTTPException): self.pc = None


ledc = LedStrip()


AEC_REF, AEC_MIC, AEC_ON = [], [], [False]  # (время, байты); AEC_ON включает запись


def music_viz(block, t):
    if live: live.push_ref(np.frombuffer(block, np.int16).reshape(-1, 3).mean(1).astype(np.int16))  # опора для эхоподавления
    if AEC_ON[0]: AEC_REF.append((t, block))
    try: viz_q.put_nowait((t, *beat_detector.feed(block, t)))
    except queue.Full: pass


def viz_loop():
    """Кольцо в такт музыке: цвет плывёт по кругу и прыгает на бите, яркость следует за энергией."""
    hue, kick, drawing, last, phase = 0.0, 0.0, False, 0.0, 0.0
    while True:
        t, level, beat = viz_q.get()
        # кадр кольца дорог (перепривязка SPI ~25 мс процессора): рисуем не чаще 10 раз в секунду, биты копим
        while t - last < 0.1 or t < time.time() - 0.05:
            try: t2, l2, b2 = viz_q.get(timeout=0.2)
            except queue.Empty: break
            t, level, beat = t2, max(level, l2) if t2 - last < 0.1 else l2, beat or b2
        last = t
        time.sleep(max(0.0, t - time.time()))
        ok = music and music.playing and not music.sus and not busy.is_set() and not awaiting_until and not muted             and time.time() > viz_block_until
        if not ok:
            if drawing and not (music and music.playing): drawing = False; ui_idle()
            drawing = drawing and ok; continue
        hue = (hue + 0.002) % 1.0
        if beat: hue = (hue + 0.09) % 1.0; kick = 1.0
        kick *= 0.82
        v = min(1.0, 0.10 + 0.50 * level + 0.50 * kick)
        if getattr(music, "kind", "") == "wave":  # «Моя волна»: жёлтое и фиолетовое, переливаются волной, бит двигает фазу
            phase += 0.1 + (1.3 if beat else 0.0)
            cols = [tuple(int(v * (a + (b - a) * (0.5 + 0.5 * math.sin(phase + i * 0.55)))) for a, b in zip((255, 190, 0), (150, 0, 255)))
                    for i in range(12)]
        else:
            cols = [tuple(int(255 * c) for c in colorsys.hsv_to_rgb((hue + i / 12 * 0.5) % 1.0, 0.95, v)) for i in range(12)]
        ring.colors(cols)
        vl = min(1.0, 0.45 + 0.55 * level + 0.4 * kick)  # лентам яркость выше, чем кольцу: они светят в комнату
        if getattr(music, "kind", "") == "wave":
            m = 0.5 + 0.5 * math.sin(phase); ledc.rgb([vl * (a + (b - a) * m) for a, b in zip((255, 190, 0), (150, 0, 255))])
        else: ledc.rgb([255 * c for c in colorsys.hsv_to_rgb(hue % 1.0, 0.95, vl)])  # лента в тон кольца
        drawing = True


def speak(text):
    """Колонка говорит от себя: сервер озвучивает фразу (/say), здесь играем."""
    global deaf_until
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=30)
        c.request("POST", "/say", text.encode("utf-8"), {**HDR, "Content-Type": "text/plain; charset=utf-8"})
        pcm = c.getresponse().read()
    except OSError: return
    deaf_until = max(deaf_until, time.time() + len(pcm) / 48000 + 0.6)
    subprocess.Popen(PLAY, stdin=subprocess.PIPE).communicate(pcm)


AECREC_BUF = []


def aec_record():
    """Сбор данных: 12 минут музыки («Моя волна») через живое подавление, каждую минуту очищенный звук в /tmp/aecrec."""
    os.makedirs("/tmp/aecrec", exist_ok=True)
    while not os.path.exists("/tmp/aecrec_go"): time.sleep(0.5)
    print(time.strftime("%H:%M:%S"), "AECREC start", music.play("", "wave"), flush=True)
    t_end, n = time.time() + 12 * 60, 0
    while time.time() < t_end:
        time.sleep(1)
        if len(AECREC_BUF) >= 3000:
            data, AECREC_BUF[:] = b"".join(AECREC_BUF), []
            with open(f"/tmp/aecrec/c{n:02d}_{int(time.time())}.raw", "wb") as f: f.write(data)
            print(time.strftime("%H:%M:%S"), f"AECREC chunk {n} ({len(data)/32000:.0f}s)", flush=True); n += 1
    music.ctl("stop"); print(time.strftime("%H:%M:%S"), "AECREC done", flush=True)


def aec_experiment():
    """Опыт: ждём калибровку, включаем музыку, 100 секунд пишем опорный сигнал и микрофон в /tmp/aec_*."""
    while not os.path.exists("/tmp/aec_go"): time.sleep(0.5)  # ждём сигнала от меня, когда человек готов говорить
    AEC_REF.clear(); AEC_MIC.clear(); AEC_ON[0] = True
    print(time.strftime("%H:%M:%S"), "AECDBG start music", music.play(CONF["AECDBG"], "auto"), flush=True)
    time.sleep(100)
    music.ctl("stop"); AEC_ON[0] = False
    with open("/tmp/aec_ref.raw", "wb") as f: f.write(b"".join(b for _, b in AEC_REF))
    with open("/tmp/aec_ref.txt", "w") as f: f.write(" ".join(f"{t:.4f}" for t, _ in AEC_REF))
    with open("/tmp/aec_mic.raw", "wb") as f: f.write(b"".join(b for _, b in AEC_MIC))
    with open("/tmp/aec_mic.txt", "w") as f: f.write(" ".join(f"{t:.4f}" for t, _ in AEC_MIC))
    print(time.strftime("%H:%M:%S"), f"AECDBG saved: ref blocks {len(AEC_REF)}, mic frames {len(AEC_MIC)}", flush=True)


def pc_call(path, body, timeout=8):
    u = urllib.parse.urlparse(CONF["PC_URL"])
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    try: c.request("POST", path, json.dumps(body).encode(), {"X-Token": CONF["PC_TOKEN"]})
    except OSError: hosts_kick.set(); raise
    return c.getresponse()


def do_pc(a):
    """Команды компьютеру (одна или несколько): колонка передаёт их исполнителю на ПК, белый список там."""
    if not CONF.get("PC_URL"): speak("Управление компьютером не настроено."); return
    for it in a.get("items") or [{"do": a.get("do"), "target": a.get("target", "")}]:
        try: r = json.loads(pc_call("/do", {"action": it.get("do"), "target": it.get("target", "")}).read() or b"{}")
        except (OSError, ValueError):
            speak("Компьютер не отвечает. Проверь, что он включён и программа запущена."); return
        print("pc:", it, "->", r, flush=True)
        if not r.get("ok"): speak("Не получилось: " + (r.get("error") or "ошибка") + "."); return
        time.sleep(0.3)


def do_screen(a):
    """«Посмотри на экран»: снимок монитора с ПК -> сервер (DeepSeek с картинкой, при нужде поиск) -> ответ голосом."""
    global player, deaf_until
    if not CONF.get("PC_URL"): speak("Управление компьютером не настроено."); return
    loading_ui()
    try:
        r = pc_call("/screenshot", {"monitor": a.get("monitor", 0)}, 20); jpg = r.read()
        if r.status != 200 or len(jpg) < 1000: raise OSError("screenshot")
    except OSError:
        speak("Не получилось сделать снимок экрана."); return
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=120)
        c.request("POST", "/vision", jpg, {**HDR, "Content-Type": "image/jpeg", "X-Question": urllib.parse.quote(a.get("question", ""))})
        r = c.getresponse()
        if r.status != 200: raise OSError("vision")
        ring.play("thinking_loop", loop=True); clock.play("eyesBlink2_digits", loop=True)
        player = p = subprocess.Popen(PLAY, stdin=subprocess.PIPE)
        first = True
        try:
            while d := r.read(8192):
                if first: clock.play("eyesHappy_digits", hold=True); first = False
                ring.show("speaking", int(rms(d[:len(d) // 2 * 2]) / 70)); p.stdin.write(d)
            p.stdin.close()
        except (BrokenPipeError, OSError): pass
        p.wait()
    except OSError:
        speak("Не получилось разобрать экран.")


def do_music(a):
    """Команда музыке от сервера: включить по запросу или управление (стоп, пауза, дальше, громкость)."""
    global music_paused
    if a["action"] == "pc": return do_pc(a)
    if a["action"] == "screen": return do_screen(a)
    if a["action"] == "live": live_start(); return True
    if a["action"] == "pcaudio":  # колонка как динамики компьютера
        if a.get("on"):
            if not (music and CONF.get("PC_URL")): speak("Компьютер не подключён."); return
            u = urllib.parse.urlparse(CONF["PC_URL"]); music_paused = False
            if music.playing: music.ctl("stop"); time.sleep(0.3)
            music.play_pc(u.hostname, u.port, CONF["PC_TOKEN"]); return True
        if music and music.src: music_paused = False; music.ctl("stop")
        return
    if a["action"] == "play":
        if not music: sound("23_request_failed"); return
        music_paused = False
        loading_ui()  # поиск и разгон потока занимают пару секунд
        r = music.play(a.get("query", ""), a.get("kind", "auto"), a.get("mood"), a.get("diversity"))
        print("music play:", r, flush=True)
        if not r.get("ok"):
            speak("Не нашёл такую музыку. Скажи название ещё раз."); ui_listen(None)  # и сразу слушаем повтор
            return True
        return True
    w = a.get("what")
    if w == "louder": volume(+1)  # (возврат None: ui_idle вызовет вызывающий)
    elif w == "quieter": volume(-1)
    elif not music: return
    elif w == "stop": music_paused = False; music.ctl("stop")
    elif w == "next": music.ctl("next")
    elif w in ("like", "dislike"): music.ctl(w)
    elif w == "previous": music.ctl("prev")
    elif w == "pause": music_paused = True
    elif w == "resume": music_paused = False


def on_wake(p, recent):
    """Детектор услышал «Скат»: свет сразу; если человек продолжает говорить, без сигнала, иначе сигнал."""
    global chime_at
    ring.color(BLUE); clock.play("eyesReady_digits", hold=True); ledc.rgb([c * 0.5 for c in THEME])  # лента загорается вместе с кольцом
    if music: threading.Thread(target=music.suspend, daemon=True).start()  # тишина и свободная звуковая карта
    threading.Thread(target=warm_up, daemon=True).start()
    start_listening(); chime_at = time.time() + 0.3
    print(f"wake p={p:.2f}", flush=True)
    if CONF.get("DEBUG"):  # последние ~2 с: разбирать ложные срабатывания и пропуски
        os.makedirs("/tmp/wakes", exist_ok=True); names = sorted(os.listdir("/tmp/wakes"))
        if len(names) > 30: os.remove(f"/tmp/wakes/{names[0]}")
        open(f"/tmp/wakes/{int(time.time())}.raw", "wb").write(b"".join(recent))


BURST = {"ring": collections.deque(maxlen=90), "base": 150.0, "len": 0, "quiet": 0, "last": 0.0}


def verify_burst(pcm):
    """Облачная проверка всплеска голоса при музыке: слышит ли Yandex «Скат» (запасной путь, когда нейросеть не уверена)."""
    try:
        c = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=20)
        c.request("POST", "/verify", normalize(pcm), {**HDR, "Content-Type": "audio/L16"})
        r = json.loads(c.getresponse().read() or b"{}")
    except (OSError, ValueError): return
    if r.get("ok") and not busy.is_set() and not awaiting_until and not muted:
        print(f"{time.strftime('%H:%M:%S')} wake via cloud: {r.get('text')!r}", flush=True)
        on_wake(1.0, [])


def burst_check(f):
    """Очищенный от музыки кадр: всплеск голоса (0,16-1,2 с громче фона в 2,2 раза) -> облачная проверка, не чаще раза в 3 с."""
    b = BURST; b["ring"].append(f); e = rms(f)
    if e > 2.2 * b["base"]:
        b["len"] += 1; b["quiet"] = 0
        return
    b["base"] += (e - b["base"]) * 0.02  # фон подстраивается только в паузах
    if b["len"]:
        b["quiet"] += 1
        if b["quiet"] >= 8:
            n, b["len"], b["quiet"] = b["len"], 0, 0
            if 8 <= n <= 60 and time.time() - b["last"] > 3.0 and len(b["ring"]) >= 80:
                b["last"] = time.time()
                threading.Thread(target=verify_burst, args=(b"".join(list(b["ring"])[-80:]),), daemon=True).start()


SURE_THR = float(CONF.get("SURE_THR", 0.985))  # выше — «Скат» без проверки; ниже — сначала спрашиваем облако, слышало ли оно слово


wake_ok, wake_busy, VCONN = threading.Event(), threading.Event(), [None]


def verify_wake_async(recent):
    """Неуверенное срабатывание: проверка идёт в потоке, цикл микрофона не ждёт; одобрено -> wake_ok."""
    if wake_busy.is_set(): return
    wake_busy.set(); frames = list(recent)

    def run():
        try:
            if verify_wake(frames): wake_ok.set()
        finally: wake_busy.clear()
    threading.Thread(target=run, daemon=True).start()


def verify_wake(recent):
    """Неуверенное срабатывание: последние ~1,2 с отправляем на распознавание; если слова «Скат» там нет, молча игнорируем (шорох, клавиши)."""
    pcm = b"".join(list(recent)[-45:])
    t0 = time.time()
    try:
        r = None
        for attempt in (0, 1):  # постоянное соединение (без TLS-рукопожатия каждый раз); сломалось — одно новое
            try:
                if VCONN[0] is None: VCONN[0] = http.client.HTTPSConnection(URL.hostname, URL.port, context=CTX, timeout=4)
                VCONN[0].request("POST", "/verify", normalize(pcm), {**HDR, "Content-Type": "audio/L16", "X-Loose": "1"})
                r = json.loads(VCONN[0].getresponse().read() or b"{}"); break
            except (OSError, http.client.HTTPException): VCONN[0] = None
        if r is None: return True
    except ValueError: return True  # нет связи с сервером: не глохнем
    print(f"{time.strftime('%H:%M:%S')} проверка срабатывания: {r.get('text')!r} -> {'да' if r.get('ok') else 'нет, игнорирую'} ({time.time() - t0:.2f} с)", flush=True)
    return bool(r.get("ok"))


def phrases():
    """Один постоянный arecord (при каждом старте он ~5 с шумит перегрузом, перезапускать нельзя).
    В покое кадры идут в детектор слова, после «Скат» VAD режет команду. Пока колонка говорит,
    думает или выключен микрофон, кадры выбрасываются."""
    global awaiting_until, chime_at
    rec = subprocess.Popen(REC, stdout=subprocess.PIPE)
    rec.stdout.read(FRAME * 2 * 500)  # ~10 с затухающего перегруза после старта arecord
    ring.play("ready"); clock.play("ready_digits"); sound("02_ready")
    pre, buf, voiced, quiet, floor, n, sm = collections.deque(maxlen=PRE), [], 0, 0, THRESH / 2, 0, 0.0
    vpeak = 450.0
    recent, wbuf, cool, was_deaf = collections.deque(maxlen=300), b"", 0.0, True
    while True:
        f = rec.stdout.read(FRAME * 2)
        if len(f) < FRAME * 2:
            raise RuntimeError("arecord завершился")
        now = time.time()
        if LIVE["on"]: live_feed(f); continue  # живой разговор: кадры идут в OpenAI, слово «Скат» и VAD не нужны
        if LIVE["ended"]:
            LIVE["ended"] = False; pre.clear(); buf, voiced, wbuf = [], 0, b""; wake.reset(); recent.clear(); was_deaf = True
        if busy.is_set() or muted or now < deaf_until:
            pre.clear(); buf, voiced, wbuf = [], 0, b""; was_deaf = True
            continue
        if was_deaf:  # после своего звука старый хвост не нужен
            wake.reset(); recent.clear(); was_deaf = False
            if live: live.reset()
        recent.append(f)
        if AEC_ON[0]: AEC_MIC.append((now, f))
        if os.path.exists("/tmp/reckeys"):  # временная запись звука комнаты (клавиатура и т.п.) для дообучения: touch /tmp/reckeys
            with open("/tmp/keys.raw", "ab") as kf: kf.write(f)
        e = rms(f)
        floor += (e - floor) * (0.002 if e > floor else 0.05)  # шум комнаты: вверх медленно, вниз быстро
        n += 1
        if not awaiting_until and wake_ok.is_set():  # облако подтвердило слово: поднимаем колонку, команда уже в последних кадрах
            wake_ok.clear(); on_wake(1.0, recent); cool = now + 2.0; sm = 0.0
            pre.clear(); pre.extend(list(recent)[-75:]); buf, voiced = [], 0
            continue
        if not awaiting_until and CONF.get("WAKE", "1") == "0":  # слово отключено (WAKE=0): в покое колонка ничего не слушает, работает только голосовая кнопка
            wbuf = b""; continue
        if not awaiting_until:  # покой: ловим слово
            if live and music and music.playing and not music.sus and not muted:  # музыка играет: вычитаем её из микрофона
                f = live.process(np.frombuffer(f, np.int16)).tobytes()
                if live.D is not None: burst_check(f)  # запасной путь через облако
                if CONF.get("AECREC") and live.D is not None: AECREC_BUF.append(f)  # запись очищенного звука для дообучения
            elif live and live.mic_n: live.reset()
            wbuf += f
            if len(wbuf) >= 2560:
                p = wake.feed(np.frombuffer(wbuf[:2560], np.int16)); wbuf = wbuf[2560:]
                if (CONF.get("AECDBG") or CONF.get("AECREC")) and p is not None and p > 0.5 and now > cool:  # режим записи: только лог вероятностей
                    print(f"{time.strftime('%H:%M:%S')} AECDBG wake-prob {p:.2f}", flush=True); cool = now + 1.0
                elif p is not None and len(wake.p) == 5 and now > cool and music and music.playing and wake.p[2:].max() > CMD_THR:
                    k = {0: 2, 1: 3, 2: 1}[int(wake.p[2:].argmax())]  # дальше -> 2 касания, назад -> 3, пауза -> 1
                    print(f"{time.strftime('%H:%M:%S')} voice-cmd {['дальше', 'назад', 'пауза'][int(wake.p[2:].argmax())]} p={wake.p[2:].max():.2f}", flush=True)
                    PAD["n"] = k; threading.Thread(target=pad_act, daemon=True).start(); cool = now + 2.5; wake.reset()
                elif p is not None and p > WAKE_THR and now > cool and p < SURE_THR:  # неуверенное срабатывание: проверяет облако, пока цикл продолжает слушать
                    verify_wake_async(recent); cool = now + 1.5
                elif p is not None and p > WAKE_THR and now > cool:
                    on_wake(p, recent); cool = now + 2.0; sm = 0.0  # громкость сглаживаем с нуля: старое значение давало вспышку красного
                    # детектор срабатывает через ~0,3 с после слова: начало команды, сказанной сразу, уже в recent
                    pre.clear(); pre.extend(list(recent)[-PRE:]); buf, voiced = [], 0
            continue
        if chime_at and now >= chime_at:  # человек замолчал после «Скат» — даём сигнал, иначе говорит дальше
            chime_at = 0
            if not buf: sound("03_wake_short"); continue
        if now > awaiting_until and not buf:  # не дождались команды
            awaiting_until = 0; chime_at = 0; ui_idle(); continue
        vg_gate = vg_update(f)  # кольцо реагирует на человеческую речь, а не на любой шум
        sm = e if e > sm else 0.72 * sm + 0.28 * e  # атака мгновенная, спад быстрый: кольцо «бьётся» в такт слогам
        vpeak = max(450.0, vpeak * 0.985, sm)  # шкала подстраивается под голос за ~секунду: обычная речь доходит почти до края
        if now > awaiting_since and n % 2 == 0:  # полосы по кругу: сколько светодиодов горит и какого цвета — зависит от громкости
            lv = max(0.0, min(1.0, (sm - 1.3 * floor) / max(1.0, vpeak - 1.3 * floor))) ** 0.7 * vg_gate  # корень: тихие слоги тоже заметны; шум без речи гасим
            col = level_color(lv); dim = tuple(int(c * 0.10) for c in THEME)
            # как заводской «слушаю»: от середины (светодиод 6, спереди) к краям; сколько светодиодов горит — зависит от громкости
            ring.colors([tuple(int(c * a) for c in col) if (a := min(1.0, lv * 7.5 + 0.5 - abs(i - 6))) > 0 else dim for i in range(12)])
            ledc.rgb([c * (0.3 + 0.7 * lv) for c in col])  # лента (и монитор) слушают вместе с кольцом: цвет и яркость идут за голосом
        loud = e > max(THRESH, 2.2 * floor)
        if not buf:
            pre.append(f); voiced = voiced + 1 if loud else 0
            if voiced >= START:
                buf, quiet, chime_at = list(pre), 0, 0
            continue
        buf.append(f); quiet = 0 if loud else quiet + 1
        if quiet >= END or len(buf) >= MAXF:
            if len(buf) - quiet >= 15:  # меньше ~0,3 с речи — шум
                yield b"".join(buf)
            buf, voiced = [], 0


# Усилитель на плате держим на максимуме (на заводе он тоже на максимуме), а громкость задаёт программная
# ступенчатая шкала в дБ: процентная шкала ALSA линейна по дБ и половина хода была бы просто тихой.
LEVELS_DB = [-46, -38, -31, -25, -20, -16, -12, -8.5, -5.5, -2.5, 0]  # 11 ступеней, одна касание = одна ступень
VOL_FILE = "/var/lib/skat-volume"


def apply_volume(i):
    # регулятор Master линеен по дБ на 0..-60: передаём процентами (значение с «-» amixer принимает за опцию)
    subprocess.run(["amixer", "-q", "-c", "0", "sset", "Master", f"{round((60 + LEVELS_DB[i]) / 60 * 100)}%"])


def volume(step):
    global vol_idx
    vol_idx = max(0, min(len(LEVELS_DB) - 1, vol_idx + step)); apply_volume(vol_idx)
    global viz_block_until
    viz_block_until = time.time() + 1.7  # на время показа громкости музыка кольцо не трогает
    ring.show("volume", round(100 * vol_idx / (len(LEVELS_DB) - 1))); sound("13_volume_touch")  # как на заводе: без цифр
    threading.Timer(1.5, lambda: ui_idle() if not awaiting_until and not busy.is_set() else None).start()
    try: open(VOL_FILE, "w").write(str(vol_idx))
    except OSError: pass
    print(f"volume step {vol_idx}/{len(LEVELS_DB)-1} ({LEVELS_DB[vol_idx]} dB)", flush=True)


PAD = {"n": 0, "t": None, "last": 0.0, "down": 0.0, "long": False, "lt": None, "t148": 0.0}


def pad_press():
    """Кнопка play/pause (148) нажата при музыке: если держат дольше 0,6 с — это «следующий трек»."""
    PAD["down"] = time.time(); PAD["long"] = False
    if PAD["lt"]: PAD["lt"].cancel()
    PAD["lt"] = threading.Timer(0.6, pad_long); PAD["lt"].start()


def pad_long():
    PAD["long"] = True; PAD["n"] = 2  # удержание = следующий трек, срабатывает сразу, не дожидаясь отпускания
    if PAD["t"]: PAD["t"].cancel()
    pad_act()


def pad_release():
    if PAD["lt"]: PAD["lt"].cancel()
    short = PAD["down"] and not PAD["long"]; PAD["down"] = 0
    if short: pad_tap()  # короткое касание


def pad_tap():
    """Короткие касания (при музыке): 1 — пауза/продолжить, 2 подряд — следующий трек, 3 — предыдущий; удержание — следующий."""
    if time.time() - PAD["last"] < 0.12: return
    PAD["last"] = time.time(); PAD["n"] += 1
    if PAD["t"]: PAD["t"].cancel()
    PAD["t"] = threading.Timer(0.7, pad_act); PAD["t"].start()


def pad_act():
    global music_paused, viz_block_until
    n, PAD["n"] = PAD["n"], 0
    if not (music and music.playing): return
    viz_block_until = time.time() + 1.6  # заводская анимация переключения, потом снова бит
    if n == 1:
        if music_paused or music.sus: music_paused = False; music.resume(); ring.play("start_track")
        else: music_paused = True; music.suspend(); ring.play("pause_track")
        clock.time()
    elif n == 2:
        ring.play("next_track"); clock.play("prevTrack_digits"); threading.Thread(target=music.ctl, args=("next",), daemon=True).start()  # на циферблате слева направо
    else:
        ring.play("prev_track"); clock.play("nextTrack_digits"); threading.Thread(target=music.ctl, args=("prev",), daemon=True).start()


def keys():
    """Сенсорные кнопки Hynitron CSK05T: struct input_event = timeval(16) + type, code, value."""
    global muted, awaiting_until, chime_at
    last_vol_key = 0.0
    with open(INPUT, "rb") as f:
        while True:
            sec, usec, typ, code, val = struct.unpack("llHHi", f.read(24))
            if typ != 1: continue
            if CONF.get("KEYDBG"): print(f"{time.strftime('%H:%M:%S')}.{int(usec / 1000):03d} raw key {code} value {val}", flush=True)  # все события, включая отпускание
            if code == 148: PAD["t148"] = time.time()
            if code == 148 and val == 0: pad_release(); continue
            if val == 0:
                continue
            print(f"{time.strftime('%H:%M:%S')} key {code} value {val}", flush=True)
            if code in (114, 115): last_vol_key = time.time()
            if code == 115: volume(+1)
            elif code == 114: volume(-1)
            elif val != 1: continue
            elif LIVE["on"] and code in (582, 148, 248):  # любая кнопка завершает живой разговор
                LIVE["play_end"] = time.time(); threading.Thread(target=live_end, daemon=True).start()
            elif code == 248:  # микрофон вкл/выкл
                muted = not muted; stop_playback(); awaiting_until = chime_at = 0
                if muted: ring.play("mic_off", hold=True); sound("05_mic_on_to_mic_off")
                else: ring.play("mic_on"); sound("06_mic_off_to_mic_on")
                try:  # мьют запоминаем: переживает перезапуск клиента
                    if muted: open(MUTE_FILE, "w").write("1")
                    elif os.path.exists(MUTE_FILE): os.remove(MUTE_FILE)
                except OSError: pass
                print(f"{time.strftime('%H:%M:%S')} {'muted' if muted else 'unmuted'}", flush=True)
            elif code in (582, 148) and time.time() - last_vol_key < 0.9: continue  # панель задевает голосовую кнопку при смене громкости
            elif code == 582 and time.time() - PAD["t148"] < 0.2: continue  # касание play/pause иногда даёт заодно 582: это тот же жест
            elif code == 148 and music and music.playing: pad_press()  # кнопка play/pause: управление треками
            elif code in (582, 148):  # слушать без «Скат», а если говорит — прервать
                if player and player.poll() is None: stop_playback(); ui_idle()
                elif awaiting_until: awaiting_until = chime_at = 0; ui_idle()  # колонка слушала — кнопка отменяет
                else:
                    if music and music.playing: threading.Thread(target=music.suspend, daemon=True).start()  # как по слову «Скат»
                    ui_listen("03_listening_start_touch")


def normalize(pcm, target=12000, max_gain=8):
    """Тихую фразу подтягиваем цифровым усилением до пика ~target: STT плохо слышит тихую речь."""
    a = array.array("h", pcm)
    peak = max(map(abs, a)) or 1
    g = min(max_gain, target / peak)
    if g <= 1.2: return pcm
    return array.array("h", (int(x * g) for x in a)).tobytes()


def handle(pcm):
    global deaf_until, awaiting_until
    t = time.time()
    awake = awaiting_until > t  # кольцо уже в режиме «слушаю»
    awaiting_until = 0
    if CONF.get("DEBUG"):  # последние фразы и их громкость
        os.makedirs("/tmp/bursts", exist_ok=True)
        if len(os.listdir("/tmp/bursts")) > 40: os.remove(f"/tmp/bursts/{sorted(os.listdir('/tmp/bursts'))[0]}")
        open(f"/tmp/bursts/{int(t)}.raw", "wb").write(pcm)
    try:
        st = send_and_play(normalize(pcm), awake, t)
        if st == 204: ui_idle()
        elif st != 200: ring.play("connection_error"); sound("16_connection_error"); clock.time()
        print(f"{len(pcm)/32000:.1f}s speech -> http {st} in {time.time()-t:.1f}s", flush=True)
    except Exception as e:
        print("error", e, flush=True); ring.play("connection_error"); sound("16_connection_error"); clock.time()
    finally:
        deaf_until = max(deaf_until, time.time() + 0.5)  # хвост эха от динамика
        busy.clear()


if __name__ == "__main__":
    subprocess.run(["amixer", "-q", "-c", "0", "sset", "Mic Boost", CONF.get("MIC_GAIN", "75")])
    subprocess.run(["amixer", "-q", "-c", "0", "sset", "Playback", "251", "on"])  # усилитель на максимум
    try: vol_idx = max(0, min(len(LEVELS_DB) - 1, int(open(VOL_FILE).read())))
    except (OSError, ValueError): vol_idx = 8
    apply_volume(vol_idx)
    if CONF.get("MUSIC_URL"):
        music = musicmod.Music(CONF["MUSIC_URL"], CONF["MUSIC_TOKEN"], PLAY_MUSIC, music_viz, float(CONF.get("MUSIC_LAG", 0.1)))
        music.on_first = music_started; music.on_fail = hosts_kick.set; music.pcbuf = int(CONF.get("PCAUDIO_BUF", 100)) * 1000
        threading.Thread(target=viz_loop, daemon=True).start()
    hosts_kick.set(); threading.Thread(target=hosts_loop, daemon=True).start()  # адреса Pi и ПК ищутся сами
    threading.Thread(target=keys, daemon=True).start()
    if CONF.get("AECDBG"): threading.Thread(target=aec_experiment, daemon=True).start()
    if CONF.get("AECREC"): threading.Thread(target=aec_record, daemon=True).start()
    if muted: ring.play("mic_off", hold=True)
    else: ring.off()
    clock.time()
    print(f"{time.strftime('%H:%M:%S')} ready (muted={muted})", flush=True)
    for pcm in phrases():
        busy.set()
        threading.Thread(target=handle, args=(pcm,), daemon=True).start()
