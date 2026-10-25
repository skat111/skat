"""Лента ZENGGE (LEDnetWF, Bluetooth LE) для Ската: держит одно соединение и принимает цвета по HTTP.
Запуск на Pi: LED_ADDR=AA:BB:CC:DD:EE:FF MUSICD_TOKEN=... python ledd.py (порт LED_PORT, по умолчанию 8781; заголовок X-Token).
POST /rgb/R/G/B  временный цвет (эмоция, бит музыки): последний выигрывает, не чаще ~12 раз в секунду
POST /base/R/G/B постоянный цвет (голос): к нему возвращает /restore;  POST /restore  /on  /off  /gain/0..100 (яркость)
GET  /state"""
import asyncio, glob, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from bleak import BleakClient, BleakScanner

E = os.environ.get
ADDR, TOKEN, PORT = E("LED_ADDR"), E("MUSICD_TOKEN"), int(E("LED_PORT", "8781"))
STATE_FILE = E("LED_STATE", "/var/lib/skat-led.json")
CH = "0000ff01-0000-1000-8000-00805f9b34fb"
seq = [0]


def wrap(cmd):
    """Транспортная обёртка протокола LEDnetWF: 00 seq 80 00 00 len len+1 0B + команда + контрольная сумма."""
    cmd = bytes(cmd); inner = cmd + bytes([sum(cmd) & 0xFF]); seq[0] = (seq[0] + 1) & 0xFF
    return bytes([0x00, seq[0], 0x80, 0x00, 0x00, len(inner), len(inner) + 1, 0x0B]) + inner


def rgb_cmd(r, g, b): return wrap([0x31, r, g, b, 0, 0, 0xF0, 0x0F])
def power_cmd(on): return wrap([0x3B, 0x23 if on else 0x24, 0, 0, 0, 0, 0, 0, 0, 0x32, 0, 0])


S = {"base": [132, 132, 132], "on": True, "gain": 1.0}  # base: цвет, который был на ленте до Ската (тусклый белый)
try: S.update(json.load(open(STATE_FILE)))
except (OSError, ValueError): pass
dev = {"on": None, "rgb": None}     # что сейчас на ленте
want = {"v": None}                  # ("rgb", (r, g, b)) | ("power", bool)
lock = threading.Lock()


SKY = {"tgt": (0, 0, 0), "cur": [0.0, 0.0, 0.0], "n": int(E("SKY_LEDS", "65")), "ok": False}


def sky_loop():
    """Лента монитора Skydimo по USB (CH340) прямо на Pi: приветствие «Moni-A», затем кадры «Ada» 00 00 N + RGB с гаммой 2.2, ~22 к/с."""
    import serial
    while True:
        dev = next(iter(sorted(glob.glob("/dev/ttyUSB*"))), None)
        if not dev: SKY["ok"] = False; time.sleep(3); continue
        try:
            ser = serial.Serial(dev, 115200, timeout=0.5, write_timeout=2); ser.dtr = ser.rts = True
            time.sleep(1.2); ser.write(b"Moni-A"); time.sleep(0.5); ser.reset_input_buffer()
            SKY["ok"] = True; print("лента монитора подключена:", dev, flush=True)
            while True:
                SKY["cur"] = [c + (t - c) * 0.55 for c, t in zip(SKY["cur"], SKY["tgt"])]
                px = bytes(round(255 * (min(255, max(0, c)) / 255) ** 2.2) for c in SKY["cur"])
                ser.write(b"Ada\x00\x00" + bytes([SKY["n"]]) + px * SKY["n"]); time.sleep(0.045)
        except Exception as e:
            SKY["ok"] = False; print("лента монитора:", repr(e)[:120], flush=True); time.sleep(3)


def save():
    try: json.dump(S, open(STATE_FILE, "w"))
    except OSError: pass


def push(item):
    with lock: want["v"] = item


def show(rgb):
    """Временный цвет: если лента выключена голосом, не трогаем."""
    if S["on"]:
        v = tuple(min(255, int(c * S["gain"])) for c in rgb); push(("rgb", v)); SKY["tgt"] = v


async def worker():
    while True:
        try:
            d = await BleakScanner.find_device_by_address(ADDR, 15)
            if not d: print("лента не найдена, повтор", flush=True); continue
            async with BleakClient(d, timeout=20) as c:
                print("лента подключена", flush=True); dev["on"] = dev["rgb"] = None
                last = 0.0
                while c.is_connected:
                    with lock: item, want["v"] = want["v"], None
                    if item is None: await asyncio.sleep(0.02); continue
                    kind, val = item
                    if kind == "power":
                        if val != dev["on"]: await c.write_gatt_char(CH, power_cmd(val), response=False); dev["on"] = val; dev["rgb"] = None
                    elif tuple(val) != dev["rgb"]:
                        await c.write_gatt_char(CH, rgb_cmd(*val), response=False); dev["rgb"] = tuple(val)  # сначала цвет, потом питание: без вспышки старого цвета
                        if dev["on"] is not True: await asyncio.sleep(0.05); await c.write_gatt_char(CH, power_cmd(True), response=False); dev["on"] = True
                    await asyncio.sleep(max(0.0, 0.08 - (time.time() - last))); last = time.time()
        except Exception as e:
            print("ble error:", repr(e)[:200], flush=True); await asyncio.sleep(3)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, body=b"ok"):
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("X-Token") != TOKEN: return self.reply(403)
        self.reply(200, json.dumps({**S, "dev": dev, "sky": SKY["ok"]}).encode())

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN: return self.reply(403)
        p = self.path.strip("/").split("/")
        try:
            if p[0] == "rgb": show([max(0, min(255, int(x))) for x in p[1:4]])
            elif p[0] == "base":
                S["base"] = [max(0, min(255, int(x))) for x in p[1:4]]; S["on"] = True; save(); show(S["base"])
            elif p[0] == "restore": show(S["base"]) if S["on"] else (push(("power", False)), SKY.update(tgt=(0, 0, 0)))
            elif p[0] == "on": S["on"] = True; save(); show(S["base"])
            elif p[0] == "off": S["on"] = False; save(); push(("power", False)); SKY["tgt"] = (0, 0, 0)
            elif p[0] == "gain": S["gain"] = max(0.03, min(1.0, int(p[1]) / 100)); save(); show(S["base"])
            else: return self.reply(404)
        except (ValueError, IndexError): return self.reply(400)
        self.reply(200)


if __name__ == "__main__":
    assert ADDR and TOKEN, "нужны LED_ADDR и MUSICD_TOKEN"
    threading.Thread(target=sky_loop, daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever, daemon=True).start()
    asyncio.run(worker())
