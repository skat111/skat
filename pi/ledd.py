"""Лента IllumiHome DMRRBA-007 по Bluetooth LE: держит одно соединение и принимает цвета по HTTP.
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
CH = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"


def rgb_cmd(r, g, b): return bytes([0x5A, 0x07, 0x01, r, g, b])
def brightness_cmd(percent): return bytes([0x5A, 0x03, 0x01, percent, 0])
def power_cmd(on): return bytes([0x5A, 0x01, 0x02, int(on)])


S = {"base": [132, 132, 132], "on": True, "gain": 1.0}  # base: цвет, который был на ленте до Ската (тусклый белый)
try: S.update(json.load(open(STATE_FILE)))
except (OSError, ValueError): pass
dev = {"on": None, "rgb": None, "gain": None}  # что сейчас на ленте
want = {"v": None}                  # ("rgb", (r, g, b)) | ("power", bool) | ("gain", 0..100)
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
        v = tuple(min(255, int(c)) for c in rgb)
        m = max(v)  # потолочная лента всегда на полной яркости: цвет сохраняем, а «яркость» не гасим (LED_FULL=0 вернёт прежнее поведение)
        push(("rgb", tuple(int(c * 255 / m) for c in v) if (E("LED_FULL", "1") == "1" and m > 0) else v))
        SKY["tgt"] = tuple(int(c * S["gain"]) for c in v)


async def worker():
    while True:
        try:
            d = await BleakScanner.find_device_by_address(ADDR, 15)
            if not d: print("лента не найдена, повтор", flush=True); continue
            async with BleakClient(d, timeout=20) as c:
                print("лента DMRRBA-007 подключена", flush=True); dev.update(on=None, rgb=None, gain=None)
                gain = round(S["gain"] * 100)
                await c.write_gatt_char(CH, brightness_cmd(gain), response=False); dev["gain"] = gain
                if S["on"]:
                    rgb = tuple(S["base"])
                    await c.write_gatt_char(CH, rgb_cmd(*rgb), response=False); dev["rgb"] = rgb
                    await c.write_gatt_char(CH, power_cmd(True), response=False); dev["on"] = True
                else:
                    await c.write_gatt_char(CH, power_cmd(False), response=False); dev["on"] = False
                last = 0.0
                while c.is_connected:
                    with lock: item, want["v"] = want["v"], None
                    if item is None: await asyncio.sleep(0.02); continue
                    kind, val = item
                    if kind == "power":
                        if val != dev["on"]: await c.write_gatt_char(CH, power_cmd(val), response=False); dev["on"] = val; dev["rgb"] = None
                    elif kind == "gain":
                        if val != dev["gain"]: await c.write_gatt_char(CH, brightness_cmd(val), response=False); dev["gain"] = val
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
            elif p[0] == "gain":
                S["gain"] = max(0.03, min(1.0, int(p[1]) / 100)); save(); push(("gain", round(S["gain"] * 100)))
                SKY["tgt"] = tuple(int(c * S["gain"]) for c in S["base"])
            else: return self.reply(404)
        except (ValueError, IndexError): return self.reply(400)
        self.reply(200)


if __name__ == "__main__":
    assert ADDR and TOKEN, "нужны LED_ADDR и MUSICD_TOKEN"
    threading.Thread(target=sky_loop, daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever, daemon=True).start()
    asyncio.run(worker())
