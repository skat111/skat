"""Кольцо подсветки (12 пикселей WS2812 по SPI) + проигрывание заводских анимаций Маруси (/opt/skat/led/*.json).

Кадр (400 байт): 128 нулей, 36 слов по 4 байта (каждый бит значения -> 4 бита SPI: 0=8, 1=c, MSB первым,
байты слова в обратном порядке из-за bits_per_word=64), 128 нулей. Раскладка каналов выведена из `ringled --dump`.
Отправка как у ringled: праймер 16 байт на 1 МГц + кадр на 3,34 МГц одним SPI_IOC_MESSAGE(2), а драйвер
meson-spicc перед КАЖДЫМ кадром перепривязывается (иначе делитель частоты не обновляется и верно горит один пиксель).
ponytail: из-за перепривязки (~25 мс) потолок ~20 кадров/с; анимации играются по реальному времени, кадры пропускаются."""
import ctypes, fcntl, json, os, struct, threading, time

SPI, DRV, DEV, LED = "/dev/spidev0.0", "/sys/bus/platform/drivers/meson-spicc", "ffd13000.spi", "/opt/skat/led"
IOC = 0x40406B00  # SPI_IOC_MESSAGE(2)
ZERO = bytes([0x88]) * 4


def word(v):
    return bytes(reversed([(0x88, 0x8C, 0xC8, 0xCC)[(v >> i) & 3] for i in (6, 4, 2, 0)]))


def frame(colors, scale=1.0):
    f = bytearray(400)
    f[128:272] = ZERO * 36
    for p, rgb in enumerate(colors):
        k, odd = divmod(p, 2)
        for w, v in zip((6 * k + 5, 6 * k + 2, 6 * k + 4) if odd else (6 * k, 6 * k + 1, 6 * k + 3), rgb):
            f[128 + 4 * w:132 + 4 * w] = word(min(255, int(v * scale)))
    return bytes(f)


def hexrgb(s):
    return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))


class Spi:
    def __init__(self):
        self.lock = threading.Lock()

    def send(self, data):
        primer, buf = ctypes.create_string_buffer(16), ctypes.create_string_buffer(data, len(data))
        x = struct.pack("<QQIIHBBBBBB", ctypes.addressof(primer), 0, 16, 1_000_000, 0, 64, 0, 0, 0, 0, 0)
        x += struct.pack("<QQIIHBBBBBB", ctypes.addressof(buf), 0, len(data), 3_340_000, 0, 64, 0, 0, 0, 0, 0)
        with self.lock:
            for f in ("unbind", "bind"):
                try:
                    with open(f"{DRV}/{f}", "w") as d: d.write(DEV)
                except OSError: pass
            for _ in range(200):  # до 1 с: после bind узел появляется не сразу
                if os.path.exists(SPI): break
                time.sleep(0.005)
            try:
                fd = os.open(SPI, os.O_RDWR)
                try: fcntl.ioctl(fd, IOC, x)
                finally: os.close(fd)
            except OSError:
                pass  # пропущенный кадр лучше, чем упавшая анимация


THEMED = {"thinking_loop", "listening_start", "listening", "loading", "ready", "speaking", "start_track", "next_track", "prev_track",
          "pause_track", "updateDownloadСomplete_diodes", "volume"}  # заводские синие анимации; красные (ошибки, микрофон) не трогаем


def fallback_frames(name):
    """Простые анимации вместо заводских: уровень (середина -> края) и мягкая пульсация; ошибки и выключенный микрофон — красные."""
    base = "#ff2020" if ("error" in name or "mic_off" in name) else "#2060ff"
    if name in ("listening", "speaking", "volume"):  # кадр выбирается уровнем голоса, а не временем
        n = 51 if name == "listening" else 101; out = []
        for k in range(n):
            lv = k / (n - 1); cols = []
            for i in range(12):
                a = max(0.0, min(1.0, lv * 7.5 + 0.5 - abs(i - 6))); c = int(255 * a)
                cols.append("#%02x%02x%02x" % tuple(int(int(base[j:j + 2], 16) * a) for j in (1, 3, 5)) if c else "#000103")
            out.append({"duration": 0, "colors": cols})
        return out
    out = []
    for k in range(20):
        a = 0.15 + 0.85 * (k / 10 if k < 10 else (20 - k) / 10)
        out.append({"duration": 0.04, "colors": ["#%02x%02x%02x" % tuple(int(int(base[j:j + 2], 16) * a) for j in (1, 3, 5))] * 12})
    return out


class Ring:
    def __init__(self, scale=1.0):
        self.spi, self.scale, self.cache, self.stop, self.t = Spi(), scale, {}, threading.Event(), None
        self.theme = None  # (r, g, b): перекрасить заводские анимации, сохранив их яркость
        self.off()

    def anim(self, name, tint=None):
        if tint is None and self.theme and name in THEMED: tint = self.theme
        if (name, tint) not in self.cache:
            try: fr = json.load(open(f"{LED}/{name}.json"))
            except OSError: fr = fallback_frames(name)  # заводского файла нет: простая своя анимация
            # duration 0 (listening) и 16.6 (volume) — кадр выбирается уровнем, а не временем
            gain = {"next_track": 2.4, "prev_track": 2.4, "pause_track": 2.0, "start_track": 2.0}.get(name, 1.0)  # заводские кадры переключения тусклые
            tc = lambda c: tuple(min(255, int(t * min(1.0, max(c) * gain / 255))) for t in tint) if tint else tuple(min(255, int(x * gain)) for x in c)  # яркость кадра сохраняем, цвет — эмоции
            self.cache[(name, tint)] = [(f["duration"] if 0 < f.get("duration", 0) < 1 else 0.0166,
                                         frame([tc(hexrgb(c)) for c in f["colors"]], self.scale)) for f in fr]
        return self.cache[(name, tint)]

    def _halt(self):
        self.stop.set()
        if self.t and self.t is not threading.current_thread(): self.t.join(2)
        self.stop = threading.Event()

    def off(self):
        self._halt(); self.spi.send(frame([(0, 0, 0)] * 12))

    def color(self, rgb):
        """Всё кольцо одним цветом (0-255 на канал)."""
        self._halt(); self.spi.send(frame([rgb] * 12, self.scale))

    def colors(self, rgbs):
        """12 цветов (r, g, b) по пикселям."""
        self._halt(); self.spi.send(frame(rgbs, self.scale))

    def show(self, name, idx, tint=None):
        """Один кадр по индексу — для анимаций, где кадр = уровень (listening, speaking, volume)."""
        fr = self.anim(name, tint); self._halt()
        self.spi.send(fr[max(0, min(len(fr) - 1, idx))][1])

    def play(self, name, loop=False, hold=False, then=None):
        """then: (имя, loop) — что играть сразу после; hold: оставить последний кадр."""
        self._halt(); stop = self.stop

        def run():
            fr = self.anim(name)
            ends, acc = [], 0.0
            for d, _ in fr: acc += d; ends.append(acc)
            start, last = time.monotonic(), -1
            while not stop.is_set():
                el = time.monotonic() - start
                if el >= acc:
                    if not loop: break
                    start, last, el = time.monotonic(), -1, 0.0
                i = next(i for i, e in enumerate(ends) if e > el)  # кадр по реальному времени
                if i != last:
                    self.spi.send(fr[i][1]); last = i
                else: time.sleep(0.004)
            if stop.is_set(): return
            self.spi.send(fr[-1][1]) if hold else None
            if then: self.play(then[0], loop=then[1])
            elif not hold: self.off()
        self.t = threading.Thread(target=run, daemon=True); self.t.start()


if __name__ == "__main__":  # самопроверка кодирования против ringled --dump (запускать на колонке)
    import random, subprocess
    for _ in range(5):
        cs = [tuple(random.randrange(256) for _ in range(3)) for _ in range(12)]
        ref = subprocess.run(["ringled", "-b", "100", "--dump", "raw", *["%02x%02x%02x" % c for c in cs]],
                             capture_output=True).stdout
        assert frame(cs) == ref, "кодирование не совпало с ringled"
    print("encoder ok")
