#!/usr/bin/env python3
"""Музыкальный сервис Ската для Raspberry Pi (российский IP: Яндекс Музыка из-за рубежа отдаёт 451).
Неофициальный API через библиотеку yandex-music + ffmpeg. Колонка тянет с него готовый PCM по домашней сети.

POST /play  {"query": "...", "kind": "auto|wave|likes"}   -> {"ok":true,"title":..,"artist":..,"count":N}
GET  /stream                                              -> chunked PCM s16le 48k mono, трек за треком
POST /ctl   {"action": "next|prev|stop"}
GET  /now                                                 -> {"title":..,"artist":..}
Заголовок X-Token = MUSICD_TOKEN. Токен аккаунта: YANDEX_MUSIC_TOKEN (без него только 30-секундные превью).
ponytail: одна очередь на весь дом, одна колонка; на несколько колонок нужны очереди по X-Device."""
import concurrent.futures, json, os, subprocess, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from yandex_music import Client

E = os.environ.get
TOKEN, YM_TOKEN, PORT = E("MUSICD_TOKEN"), E("YANDEX_MUSIC_TOKEN"), int(E("MUSICD_PORT", "8780"))
client = Client(YM_TOKEN).init() if YM_TOKEN else Client().init()
lock = threading.Lock()
Q = {"tracks": [], "i": 0, "station": None, "batch": None, "now": None, "cur": None}
fpool = concurrent.futures.ThreadPoolExecutor(1)  # обратная связь Яндексу (для «Моей волны»), не задерживает музыку
STATIONS = {}


def find_station(query):
    """Жанровая/настроенческая станция Яндекса по названию («русский рок», «джаз», «медитация»)."""
    if not STATIONS:
        for s in client.rotor_stations_list():
            try: STATIONS[s.station.name.lower()] = f"{s.station.id.type}:{s.station.id.tag}"
            except AttributeError: pass
    q = query.lower().strip()
    stem = q[: max(4, len(q) - 2)]  # «медитация» ~ «медитации»: сравниваем без окончания
    for name, sid in STATIONS.items():
        if q and (q in name or name in q or stem in name): return sid
    import difflib
    m = difflib.get_close_matches(q, list(STATIONS), n=1, cutoff=0.5)
    return STATIONS[m[0]] if m else None


def feedback(kind, t, secs=0.0):
    st, batch = Q["station"], Q["batch"]
    if not st: return

    def go():
        try:
            if kind == "started": client.rotor_station_feedback_track_started(st, t.id, batch_id=batch)
            elif kind == "finished": client.rotor_station_feedback_track_finished(st, t.id, secs, batch_id=batch)
            else: client.rotor_station_feedback_skip(st, t.id, secs, batch_id=batch)
        except Exception as e: print("feedback error", e, flush=True)
    fpool.submit(go)
skip = {"n": 0}  # +1 следующий, -1 предыдущий: поток сам переключается без разрыва соединения
stream_id = {"n": 0}  # новый /stream вытесняет старый


def music_only(tracks):
    """Только музыка: подкасты, аудиокниги и радиопередачи (там «просто говорит мужик») отбрасываем."""
    return [t for t in tracks if getattr(t, "type", None) in (None, "music") and t.available is not False]


def matches(query, t):
    """Хватает ли слов запроса в названии и артистах трека (чтобы не включать случайное)."""
    words = [w for w in query.lower().split() if len(w) > 2]
    text = (t.title + " " + " ".join(a.name for a in t.artists)).lower()
    return not words or sum(w in text for w in words) / len(words) >= 0.5


def full(t):
    return t.fetch_track() if hasattr(t, "fetch_track") else t


def resolve(query, kind, mood=None, diversity=None):
    """Список треков по запросу: артист -> его хиты, альбом, плейлист, трек, «Моя волна» (+настроение), станция, любимое.
    Возвращает (треки, станция или None)."""
    if kind in ("wave", "station") or not query.strip():
        station = find_station(query) if kind == "station" else "user:onyourwave"
        if not station: return [], None
        if station == "user:onyourwave" and (mood or diversity):
            try: client.rotor_station_settings2(station, mood or "all", diversity or "default", "any")
            except Exception as e: print("wave settings error", e, flush=True)
        q = client.rotor_station_tracks(station)
        Q["batch"] = q.batch_id
        try: client.rotor_station_feedback_radio_started(station, from_="skat", batch_id=q.batch_id)
        except Exception as e: print("radio_started error", e, flush=True)
        return [full(s.track) for s in q.sequence], station
    if kind == "likes":
        return [full(t) for t in client.users_likes_tracks().tracks[:100]], False
    r = client.search(query, type_="all")
    b = r.best
    try:
        if b and b.type == "artist":  # сначала самые популярные треки артиста, потом остальные
            pop = client.artists_brief_info(b.result.id).popular_tracks or []
            rest = client.artists_tracks(b.result.id, page_size=40).tracks
            seen, out = set(), []
            for t in [*pop, *[full(x) for x in rest]]:
                if t.id not in seen: seen.add(t.id); out.append(t)
            return music_only(out), False
        if b and b.type == "album":
            alb = client.albums_with_tracks(b.result.id)
            return music_only([t for v in alb.volumes for t in v]), False
        if b and b.type == "playlist":
            return music_only([full(t) for t in b.result.fetch_tracks()[:100]]), False
        if b and b.type == "track" and matches(query, b.result):  # «лучший» результат без общих слов с запросом не берём
            return music_only([b.result] + [t for t in (r.tracks.results if r.tracks else []) if t.id != b.result.id][:15]), False
    except Exception as e:
        print("resolve fallback:", e, flush=True)
    cand = music_only(list(r.tracks.results[:20]) if r.tracks else [])
    return (cand if cand and matches(query, cand[0]) else []), False  # случайное первое попавшееся не берём


def resolve_loose(query, kind, mood=None, diversity=None):
    """Распознавание речи часто путает названия: если по запросу пусто, пробуем без последних слов (до двух)."""
    words = query.split()
    for cut in (0, 1, 2):
        if kind != "auto" or len(words) - cut < 1: break
        q = " ".join(words[: len(words) - cut])
        tracks, station = resolve(q, kind, mood, diversity)
        if tracks: return tracks, station
    return resolve(query, kind, mood, diversity) if kind != "auto" or not words else ([], None)


def link(t):
    infos = sorted(t.get_download_info(), key=lambda i: (i.codec == "mp3", i.bitrate_in_kbps), reverse=True)
    return infos[0].get_direct_link()


pool = concurrent.futures.ThreadPoolExecutor(2)
PRE = {}  # id трека -> future со скачанным файлом


def download(t):
    """Трек целиком во временный файл: CDN Яндекса рвёт соединение, если читать медленно (ffmpeg в таком случае
    молча обрезал трек на ~40-й секунде)."""
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="skat-"); os.close(fd)
    with urllib.request.urlopen(link(t), timeout=30) as r, open(path, "wb") as f:
        while d := r.read(65536): f.write(d)
    return path


def fetch(t):
    return (PRE.pop(t.id, None) or pool.submit(download, t)).result(timeout=90)


def prefetch(t):
    if t.id not in PRE: PRE[t.id] = pool.submit(download, t)


def title(t): return {"title": t.title, "artist": ", ".join(a.name for a in t.artists)}


def next_wave():
    last = Q["tracks"][-1] if Q["tracks"] else None
    q = client.rotor_station_tracks(Q["station"], queue=f"{last.id}" if last else None)
    Q["batch"] = q.batch_id or Q["batch"]
    return [full(s.track) for s in q.sequence]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def reply(self, code, obj=None):
        b = json.dumps(obj or {}, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.headers.get("X-Token") != TOKEN: return self.reply(403)
        if self.path == "/now": return self.reply(200, Q["now"] or {})
        if self.path != "/stream": return self.reply(404)
        stream_id["n"] += 1; me = stream_id["n"]
        self.send_response(200); self.send_header("Content-Type", "audio/L16")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        try:
            # ffmpeg отдаёт звук быстрее воспроизведения; без этого в буферах сокета копятся секунды, и «дальше» срабатывало с задержкой ~10 с
            self.connection.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_SNDBUF, 32768)
            while me == stream_id["n"]:
                with lock:
                    if skip["n"]: Q["i"] = max(0, Q["i"] + skip["n"] - 1); skip["n"] = 0
                    if Q["i"] >= len(Q["tracks"]) and Q["station"]:
                        try: Q["tracks"] += next_wave()
                        except Exception as e: print("wave error", e, flush=True)
                    if Q["i"] >= len(Q["tracks"]): break
                    t = Q["tracks"][Q["i"]]; Q["i"] += 1; Q["now"] = title(t); Q["cur"] = t
                print("playing:", Q["now"], flush=True)
                try: path = fetch(t)
                except Exception as e: print("download error", e, flush=True); continue
                with lock:  # следующий трек качаем заранее, чтобы между треками не было паузы
                    if Q["i"] < len(Q["tracks"]): prefetch(Q["tracks"][Q["i"]])
                feedback("started", t)
                sent, natural = 0, False
                ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while me == stream_id["n"] and not skip["n"]:
                        d = ff.stdout.read(16384)
                        if not d: natural = True; break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(d), d)); self.wfile.flush(); sent += len(d)
                finally:
                    ff.kill(); ff.wait()
                    try: os.remove(path)
                    except OSError: pass
                if natural: feedback("finished", t, sent / 96000)
                elif skip["n"]: feedback("skip", t, sent / 96000)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN: return self.reply(403)
        d = self.body()
        if self.path == "/play":
            try: tracks, station = resolve_loose(d.get("query", ""), d.get("kind", "auto"), d.get("mood"), d.get("diversity"))
            except Exception as e: print("play error", e, flush=True); return self.reply(502, {"ok": False, "error": str(e)[:100]})
            if not tracks: return self.reply(200, {"ok": False, "error": "ничего не найдено"})
            with lock: Q.update(tracks=tracks, i=0, station=station, now=title(tracks[0])); skip["n"] = 0
            return self.reply(200, {"ok": True, "count": len(tracks), **title(tracks[0])})
        if self.path == "/ctl":
            a = d.get("action")
            if a == "next": skip["n"] = 1
            elif a == "prev": skip["n"] = -1  # i уже указывает на следующий: назад на два
            elif a == "stop": stream_id["n"] += 1
            elif a in ("like", "dislike") and Q["cur"] is not None:
                try: (client.users_likes_tracks_add if a == "like" else client.users_dislikes_tracks_add)(Q["cur"].id)
                except Exception as e: print("like error", e, flush=True)
                if a == "dislike": skip["n"] = 1
            return self.reply(200, {"ok": True})
        self.reply(404)


if __name__ == "__main__":
    assert TOKEN, "нужен MUSICD_TOKEN"
    print("account:", client.me.account.login if client.me and client.me.account else "anonymous", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
