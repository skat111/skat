"""Циферблат (4 семисегментные цифры + 2 точки двоеточия): время, заводские анимации (*_digits.json), число.

Каждый сегмент — отдельный светодиод /sys/class/leds/clock:dN:a..g, двоеточие clock:colon:top/bottom.
Порядок 30 каналов в заводских JSON выведен из формы глаз: d1(0-6) d2(7-13) colon:top(14) colon:bottom(15) d3(16-22) d4(23-29).
Яркость следует за датчиком освещённости, как в заводском adjustCapsulaMiniClockBrightness (пороги подобраны на глаз)."""
import json, random, threading, time

LED, SYS = "/opt/skat/led", "/sys/class/leds"
LUX = "/sys/bus/iio/devices/iio:device0/in_illuminance_input"
# Плата повёрнута на 180°: слева направо физически d4 d3 d2 d1, сегменты a<->d, b<->e, c<->f, двоеточие вверх ногами.
# Каналы ниже — в «прямом» виде, как в заводских JSON; здесь они переводятся в реальные светодиоды.
ROT = dict(zip("abcdefg", "defabcg"))
NAMES = [f"clock:d{d}:{ROT[s]}" for d in (4, 3) for s in "abcdefg"] + ["clock:colon:bottom", "clock:colon:top"] + \
        [f"clock:d{d}:{ROT[s]}" for d in (2, 1) for s in "abcdefg"]
FONT = {"0": 0x3F, "1": 0x06, "2": 0x5B, "3": 0x4F, "4": 0x66, "5": 0x6D, "6": 0x7D, "7": 0x07, "8": 0x7F,
        "9": 0x6F, " ": 0, "-": 0x40}


def segs(ch):
    m = FONT.get(ch, 0)
    return [255 if m >> i & 1 else 0 for i in range(7)]


def text_vals(s, colon=False):
    s = s.rjust(4)[-4:]
    return segs(s[0]) + segs(s[1]) + [255 if colon else 0] * 2 + segs(s[2]) + segs(s[3])


# Контур дисплея по часовой: верх d1..d4 (a), правый край d4 (b,c), низ d4..d1 (d), левый край d1 (e,f) — индексы каналов
_CH = lambda d, s: {1: 0, 2: 7, 3: 16, 4: 23}[d] + "abcdefg".index(s)
PATH = [_CH(d, "a") for d in (1, 2, 3, 4)] + [_CH(4, "b"), _CH(4, "c")] + [_CH(d, "d") for d in (4, 3, 2, 1)] + [_CH(1, "e"), _CH(1, "f")]


def snake(k, tail=5):
    """Змейка по контуру: голова ярче всего, хвост гаснет; k — шаг."""
    v = [0] * 30
    for j in range(tail): v[PATH[(k - j) % len(PATH)]] = int(255 * (1 - j / tail) ** 1.5)
    return v


def eyes(l, r, top=False, bot=True):
    """Лицо: глаза — крайние цифры d1 и d4 (сегменты a..g), точки двоеточия — рот."""
    v = [0] * 30
    for c in l: v[_CH(1, c)] = 255
    for c in r: v[_CH(4, c)] = 255
    v[14], v[15] = 255 if top else 0, 255 if bot else 0
    return v


BLINK = eyes("g", "g")
OPEN = eyes("abfg", "abfg")


def face(l, r=None, top=False, bot=True, blink=True):
    """Кадры лица: поза с редким морганием."""
    p = eyes(l, r or l, top, bot)
    return [(3.2, p), (0.12, BLINK)] if blink else [(1.0, p)]


FACES = {  # поза глаз для каждой эмоции
    "calm": face("abfg"), "joy": face("abf", top=True), "surprise": face("abcdef", blink=False),
    "anger": face("fgc", "bge", blink=False), "sad": face("egb", "gfc"), "tired": face("cdeg"),
    "irony": face("abfg", "g"),
    "laugh": [(0.14, eyes("abf", "abf", True)), (0.14, eyes("abfg", "abfg", True, False))],
}
IDLE = {"wink": [(0.25, OPEN), (0.45, eyes("abfg", "g")), (0.25, OPEN)], "blink": [(0.1, BLINK), (0.15, OPEN)],
        "look": [(0.3, eyes("abfg", "abfg", False, False)), (0.5, eyes("abf", "abf", True)), (0.2, OPEN)]}


class Clock:
    def __init__(self, mult=1.0):
        self.fh = [open(f"{SYS}/{n}/brightness", "w") for n in NAMES]
        self.cur, self.mult, self.scale, self.cache = [None] * 30, mult, 0.3, {}
        self.job, self.lock, self.idle_at = ("time",), threading.Lock(), time.monotonic() + 120
        threading.Thread(target=self._light, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()

    def _put(self, vals):
        for i, v in enumerate(vals):
            v = min(255, int(v * self.scale * self.mult))
            if v != self.cur[i]:
                self.fh[i].write(str(v)); self.fh[i].flush(); self.cur[i] = v

    def _light(self):
        while True:
            try: lux = float(open(LUX).read())
            except (OSError, ValueError): lux = 100.0
            target = 0.08 if lux < 2 else 0.15 if lux < 10 else 0.3 if lux < 50 else 0.6 if lux < 200 else 1.0
            self.scale += (target - self.scale) * 0.5  # без резких скачков
            time.sleep(3)

    def anim(self, name):
        if name not in self.cache:
            try: self.cache[name] = [(f["duration"], f["brightness"]) for f in json.load(open(f"{LED}/{name}.json"))]
            except OSError: self.cache[name] = [(0.3, [0] * 30)]  # заводской анимации нет: пустой кадр, дальше покажется время
        return self.cache[name]

    def _set(self, *job):
        with self.lock: self.job = (*job, time.monotonic())

    def time(self): self._set("time")
    def spin(self): self._set("spin")
    def face(self, name): self._set("face", name if name in FACES else "calm")
    def play(self, name, loop=False, hold=False): self._set("anim", name, loop, hold)
    def text(self, s, secs=1.5, colon=False): self._set("text", s, colon, secs)

    def _run(self):
        while True:
            with self.lock: job = self.job
            kind, t0 = job[0], job[-1]
            if kind == "time":
                lt = time.localtime()
                v = text_vals(f"{lt.tm_hour}{lt.tm_min:02d}", colon=time.time() % 1 < 0.5)
                self._put(v); time.sleep(0.1)
                if time.monotonic() > self.idle_at:  # в покое колонка иногда подмигивает: ей скучно
                    self.idle_at = time.monotonic() + random.uniform(120, 300)
                    if self.scale >= 0.3:
                        for d, f in random.choice(list(IDLE.values())):
                            self._put(f); time.sleep(d)
            elif kind == "face":
                fr = FACES[job[1]]; el = (time.monotonic() - t0) % sum(d for d, _ in fr); acc = 0.0
                for d, v in fr:
                    acc += d
                    if acc > el: self._put(v); break
                time.sleep(0.03)
            elif kind == "spin":
                self._put(snake(int((time.monotonic() - t0) * 11))); time.sleep(0.03)
            elif kind == "text":
                self._put(text_vals(job[1], job[2]))
                if time.monotonic() - t0 > job[3]:
                    with self.lock:
                        if self.job is job: self.job = ("time", time.monotonic())
                time.sleep(0.05)
            elif kind == "anim":
                _, name, loop, hold, _ = job
                fr = self.anim(name); tot = sum(d for d, _ in fr)
                el = time.monotonic() - t0
                if loop: el %= tot
                if el >= tot:
                    self._put(fr[-1][1])
                    if not hold:
                        with self.lock:
                            if self.job is job: self.job = ("time", time.monotonic())
                    else: time.sleep(0.1)
                    continue
                acc = 0.0
                for d, vals in fr:
                    acc += d
                    if acc > el: self._put(vals); break
                time.sleep(0.01)
