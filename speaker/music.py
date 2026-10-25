"""Музыка на колонке: PCM-поток с musicd (Raspberry Pi, российский IP) -> aplay.
Звуковая карта открывается одним процессом (софт-громкость без dmix), поэтому на время ответа ассистента
музыка отпускает устройство: suspend() убивает aplay и перестаёт читать поток (TCP притормаживает musicd),
resume() запускает aplay заново и продолжает с того же места (теряется <0,5 с звука в буфере)."""
import fcntl, http.client, json, subprocess, threading, time
from urllib.parse import urlparse


class Music:
    def __init__(self, url, token, play_cmd, viz=None, lag=0.1):
        u = urlparse(url); self.host, self.port, self.hdr, self.cmd = u.hostname, u.port or 80, {"X-Token": token}, play_cmd
        self.gen, self.proc, self.sus, self.playing = 0, None, False, False
        self.viz, self.lag = viz, lag  # viz(блок PCM 40 мс, время звучания)
        self.kind = "auto"  # что играет: wave/station/likes/auto (подсветка «Моей волны» другая)
        self.on_first = None  # вызывается один раз, когда пошёл звук нового потока
        self.pcbuf = 120000  # буфер aplay для звука ПК, мкс (меньше — меньше задержка, но чувствительнее к рывкам сети)
        self.src = None  # (host, port, заголовки) — иначе поток идёт с musicd; сейчас звук ПК
        self.on_fail = None  # вызывается, когда Pi не отвечает (колонка ищет его по сети заново)

    def _call(self, path, obj):
        c = http.client.HTTPConnection(self.host, self.port, timeout=40)
        c.request("POST", path, json.dumps(obj).encode(), {**self.hdr, "Content-Type": "application/json"})
        return json.loads(c.getresponse().read() or b"{}")

    def play(self, query, kind="auto", mood=None, diversity=None):
        """Найти и включить; возвращает словарь musicd ({"ok":..,"title":..,"artist":..})."""
        self.src = None
        try: r = self._call("/play", {"query": query, "kind": kind, "mood": mood, "diversity": diversity})
        except (OSError, ValueError) as e:
            if self.on_fail: self.on_fail()
            return {"ok": False, "error": str(e)}
        if r.get("ok"): self.kind = kind; self._start()
        return r

    def play_pc(self, host, port, token):
        """Играть звук ПК (агент на ПК, /audio)."""
        self.src = (host, port, {"X-Token": token}); self.kind = "pc"; self._start()

    def _open(self):
        host, port, hdr = self.src or (self.host, self.port, self.hdr)
        c = http.client.HTTPConnection(host, port, timeout=60)
        if self.src:  # живой поток: мелкий приёмный буфер, иначе в сокете копятся секунды задержки
            import socket
            c.connect(); c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        c.request("GET", "/audio" if self.src else "/stream", headers=hdr); return c, c.getresponse()

    def _start(self):
        self._kill(); self.gen += 1; self.sus = False; self.playing = True
        threading.Thread(target=self._run, args=(self.gen,), daemon=True).start()

    def _kill(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None: p.kill(); p.wait()

    def _run(self, gen):
        try:
            c, r = self._open()
            first = True
            vb = b""
            tl = None  # когда зазвучит очередной блок: по байтам от начала, а не по моменту записи в aplay (там буфер)
            while gen == self.gen:
                if self.sus:
                    self._kill(); tl = None
                    while self.sus and gen == self.gen: time.sleep(0.05)
                    if self.src and gen == self.gen: c.close(); c, r = self._open()  # звук ПК живой: после ответа берём «сейчас», а не накопленное
                    continue
                d = r.read(3840 if self.src else 16384)  # звук ПК читаем мелко: каждый лишний килобайт — это задержка
                if not d: break
                if self.viz:
                    now = time.time()
                    if tl is None or now > tl + 0.3: tl = now + self.lag  # после паузы или затыка сети начинаем заново
                    vb += d  # остаток не теряем: опорный сигнал должен быть непрерывным (иначе эхо не вычесть)
                    n = len(vb) // 3840 * 3840
                    for i in range(0, n, 3840): self.viz(vb[i:i + 3840], tl); tl += 0.04
                    vb = vb[n:]
                if self.proc is None:
                    self.proc = subprocess.Popen(self.cmd + (["-B", str(self.pcbuf), "-F", str(self.pcbuf // 4)] if self.src else []), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    if self.src:  # канал на 64 КБ = 0,67 с тихой задержки: у живого звука его сжимаем до одной страницы, лишнее задерживает сеть, а не память
                        try: fcntl.fcntl(self.proc.stdin.fileno(), 1031, 4096)  # F_SETPIPE_SZ
                        except OSError: pass
                try: self.proc.stdin.write(d)
                except (BrokenPipeError, OSError): self._kill()
                if first and self.on_first: first = False; self.on_first()
        except (OSError, http.client.HTTPException) as e:
            print("music stream error", e, flush=True)
        finally:
            if gen == self.gen:
                if self.proc and self.proc.stdin and not self.sus:
                    try: self.proc.stdin.close(); self.proc.wait(5)
                    except (OSError, subprocess.TimeoutExpired): pass
                self._kill(); self.playing = False

    def suspend(self):
        """Отпустить звуковую карту (перед ответом ассистента). Ждём, пока aplay действительно умрёт."""
        if not self.playing or self.sus: return
        self.sus = True
        for _ in range(30):
            if self.proc is None: return
            time.sleep(0.02)
        self._kill()

    def resume(self):
        self.sus = False

    def ctl(self, action):
        """stop | next | prev."""
        if action == "stop":
            self.gen += 1; self.sus = False; self.playing = False; self._kill()
            if self.src: self.src = None; return  # звук ПК: у musicd останавливать нечего
            try: self._call("/ctl", {"action": "stop"})
            except (OSError, ValueError): pass
        elif self.playing and not self.src:
            try: self._call("/ctl", {"action": action})
            except (OSError, ValueError): pass
