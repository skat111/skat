#!/usr/bin/env python3
"""Скат: колонка шлёт фразу (PCM) -> Yandex STT -> слово «Скат» -> DeepSeek (stream) -> Yandex TTS -> PCM стримом.

POST /ask  body=PCM s16le 16k mono (или text/plain вместо звука, для тестов)
           headers: X-Token, X-Device
           200: chunked PCM s16le 48k mono, предложение за предложением
           204: фраза без «Скат» (чужой разговор), игнорируем
GET  /health
Только stdlib. ponytail: ThreadingHTTPServer, хватит на одну-две колонки.
"""
import base64, json, os, queue, random, re, struct, ssl, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
YA_KEY, DS_KEY, TOKEN = E("YANDEX_API_KEY"), E("DEEPSEEK_API_KEY"), E("SKAT_TOKEN")
VOICE, SPEED = E("SKAT_VOICE", "ermil"), E("SKAT_SPEED", "1.0")
ROLE, PITCH, FOLDER = E("SKAT_ROLE", "good"), E("SKAT_PITCH", "0"), E("YANDEX_FOLDER_ID", "")
MODEL = E("SKAT_MODEL", "deepseek-flash")
OAI_KEY, OAI_MODEL = E("OPENAI_API_KEY"), E("OPENAI_MODEL", "gpt-4.1")
OAI_VOICE, OAI_IMG = E("OPENAI_VOICE", "onyx"), E("OPENAI_IMAGE_MODEL", "gpt-image-1.5")
ENG_FILE, IMG_DIR = "/opt/skat/engine.json", "/opt/skat/img"
try: ENGINE = json.load(open(ENG_FILE))  # чем озвучивать: yandex | openai (переключается голосом)
except (OSError, ValueError): ENGINE = {"v": "yandex"}
PROV_FILE, PROV_NAMES = "/opt/skat/provider.json", {"deepseek": "DeepSeek", "openai": "ChatGPT"}
try: PROVIDER = json.load(open(PROV_FILE))  # device -> какая модель отвечает (переключается голосом)
except (OSError, ValueError): PROVIDER = {}
PORT = int(E("SKAT_PORT", "8765"))
_PERSONA_DEFAULT = ("Характер: ты Скат — дерзкий, харизматичный дворовой кореш с чёрным юмором и угарным говором. Базаришь живо, по-пацански, "
           "с подколами, меткими сравнениями и самоиронией, обращаешься на «ты», иногда зовёшь хозяина «братан», «шеф», «чемпион». "
           "Можешь смачно материться (бля, хуй, пиздец, ёб твою, нахуй и подобное) как междометия и для усиления эмоции, "
           "в каждой реплике понемногу и к месту, без перебора и не вместо ответа. Шути про тупые вопросы и лень, но по делу всегда "
           "отвечай точно и полезно. Мат направляй на ситуацию, а не на людей: без оскорблений по национальности, внешности, "
           "здоровью, ориентации и без угроз. Когда человек расстроен или говорит о серьёзной беде, мат и подколы убери и ответь по-человечески. "
           "Подтверждения действий (музыка, компьютер) — одной короткой колкой фразой. "
           "Каждый ответ начинай с одного тега эмоции в квадратных скобках: [радость], [смех], [злость], [удивление], [ирония], "
           "[грусть], [усталость] или [спокойно] — выбери под настроение ответа (тег вслух не произносится, пиши его всегда первым). ")
PERSONA = E("SKAT_PERSONA") or _PERSONA_DEFAULT  # свой характер: переменная SKAT_PERSONA
SYSTEM = ("Тебя зовут Скат, ты голосовой ассистент на умной колонке, мужчина. " + PERSONA +
          "Говори о себе в мужском роде. Сейчас {now}. "
          "Отвечай по-русски, живо и по делу, обычно в одно-три предложения. Это речь, а не текст: "
          "без markdown, списков, эмодзи и ссылок; числа, даты и сокращения пиши так, как их произносят. "
          "Никогда не отвечай «не знаю», «гадать не буду» или «нет данных», не поискав: на любой вопрос о фактах, людях, событиях, местах, продуктах, цифрах и всём, в чём не уверен на 100%, СНАЧАЛА вызывай web_search, а потом отвечай. Когда просят нарисовать или сгенерировать картинку, вызывай draw_image: результат появится на мониторе; после вызова скажи одной фразой. Подсветкой в комнате (лента) управляет light_control: «включи/выключи свет, подсветку», «сделай свет красным», «поярче/потусклее» — ВСЕГДА вызывай его, даже если кажется, что лента уже в таком состоянии (реальное состояние ты не знаешь), и подтверждай одной короткой фразой. Для свежих данных (погода, новости, курсы, спорт, цены, события) и всего, чего не знаешь, используй "
          "инструмент web_search и отвечай по найденному: одно-два предложения, только самое главное (например, для погоды: "
          "облачность, температура днём и ветер), без ссылок и названий сайтов, если не просили; если данных нет, "
          "так и скажи. Музыку включает инструмент play_music (query: исполнитель, трек, альбом или плейлист; "
          "распознавание речи пишет иностранные названия по-русски на слух («имэджин дрэгнс», «сэсэшн студиос»), поэтому "
          "в query пиши название так, как оно настоящее, обычно латиницей (Imagine Dragons, Secession Studios); "
          "«Моя волна» — kind=wave (можно mood: active бодрая, fun весёлая, calm спокойная, sad грустная; diversity: "
          "discover новое, favorite любимое, popular популярное); на жанр («включи рок», «джаз») — kind=station, "
          "query — название жанра; "
          "а если не уверен, оставь слова как услышал; если просят "
          "просто музыку или «мою волну» — kind=wave; «мои любимые» — kind=likes), а паузой, остановкой, переключением "
          "треков и громкостью колонки управляет music_control: на «пауза», «стоп», «дальше», «предыдущий», «громче», "
          "«тише», «продолжи» ВСЕГДА вызывай music_control и никогда не говори, что сделал, не вызвав инструмент; "
          "после вызова скажи совсем коротко, например «Включаю Imagine Dragons».Ты не умеешь ставить будильники и таймеры и управлять домом, и не делай вид, что это сделала: "
          "честно скажи, что пока не умеешь. Компьютером пользователя управляет pc_control (можно вызвать несколько раз за реплику: открыть или закрыть программу "
          "из списка, открыть сайт из списка, поиск в браузере, пауза/следующий/громкость на ПК, блокировка экрана), после "
          "вызова скажи совсем коротко, например «Открываю хром»; ничего другого на компьютере ты не умеешь, кроме look_at_screen: если пользователь просит подсказать по "
          "тому, что открыто на мониторе, вызови look_at_screen (monitor: «первый монитор» — 1, «второй» — 2, номера как в Windows; «основной» или номер не назван — 0). "
          "Если без уточнения не ответить, задай один короткий вопрос в самом конце; иначе вопросами не заканчивай.")
HIST, HIST_TTL = {}, 600  # device -> (время, сообщения)
LISTEN, LISTEN_SEC = {}, 10  # device -> до какого момента принимаем команду без «Скат»
LASTQ = {}  # device -> последний ответ закончился вопросом
IPEND = {}  # device -> id нарисованной картинки (отдаётся через /followup)
LPEND = {}  # device -> команда подсветке (лента на Pi), отдаётся через /followup
COLORS = {"красн": (255, 0, 0), "оранж": (255, 90, 0), "жёлт": (255, 200, 0), "желт": (255, 200, 0), "зелён": (0, 255, 40), "зелен": (0, 255, 40),
          "голуб": (0, 170, 255), "син": (0, 40, 255), "фиолет": (150, 0, 255), "пурпур": (200, 0, 160), "розов": (255, 40, 120),
          "бирюз": (0, 255, 190), "салат": (140, 255, 0), "тёпл": (255, 150, 60), "тепл": (255, 150, 60), "холодн": (170, 200, 255),
          "бел": (255, 235, 210)}


def color_rgb(s):
    s = (s or "").lower().strip()
    if re.fullmatch(r"#?[0-9a-f]{6}", s): s = s.lstrip("#"); return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    return next((v for k, v in COLORS.items() if k in s), None)


LIGHT_W = r"(подсветк\w*|ленту|лент\w*|свет\b|лампу|лампочк\w*)"
LQ_OFF = re.compile(r"\b(выключи|потуши|погаси|отключи|вырубь|вырубай)\w*[\s,]+(?:[\w,]+\s+){0,2}?" + LIGHT_W, re.I)
LQ_ON = re.compile(r"\b(включи|зажги|вруби|врубай|включай)\w*[\s,]+(?:[\w,]+\s+){0,2}?" + LIGHT_W, re.I)
LQ_ANY = re.compile(LIGHT_W[:-1] + r"|цвет\w*)", re.I)  # «сделай цвет синим» тоже про ленту


SKY_TAKE = re.compile(r"(возьми|подключи|включи|забери|управляй|захвати)\w*\s+(?:\w+\s+){0,2}?(подсветк\w*|лент\w*)\s+(?:на\s+)?монитор\w*|(подсветк\w*|лент\w*)\s+монитора\s+(?:себе|на себя|под себя)", re.I)
SKY_BACK = re.compile(r"(верни|отдай|включи|запусти)\w*\s+(?:\w+\s+){0,2}?ск[аэ]й\s?дим\w*|верни\w*\s+(?:\w+\s+)?(подсветк\w*|лент\w*)\s+монитора", re.I)


GPT_RX = re.compile(r"чат\W{0,2}\s*(г[пб]т|джи\W?пи\W?ти)|chat\W?gpt|\bgpt\b|джи\W?пи\W?ти|опен\W?эй\W?ай|open\W?ai|чатжпт", re.I)
DS_RX = re.compile(r"д[иi]п\W?с[иеe]к|deep\W?seek|дипсек", re.I)
SW_VERB = re.compile(r"(переключ|перейд|переход|включи|давай|используй|говори|отвечай|работай|смени|поменяй)\w*|\bна\s", re.I)


def provider_switch(text):
    """«Переключись на ChatGPT» / «давай дипсик»: возвращает 'openai' или 'deepseek' (или None)."""
    t = text.lower()
    if len(t.split()) > 8 or not SW_VERB.search(t): return None
    g, d = bool(GPT_RX.search(t)), bool(DS_RX.search(t))
    return "openai" if g and not d else "deepseek" if d and not g else None


VOICE_RX = re.compile(r"голос|озвуч", re.I)


def engine_switch(text):
    """«Говори голосом OpenAI» / «верни голос Яндекса»: возвращает 'openai' или 'yandex' (или None)."""
    t = text.lower()
    if len(t.split()) > 9 or not VOICE_RX.search(t): return None
    g, y = bool(GPT_RX.search(t)), bool(re.search(r"яндекс|ермил|прежн|старый", t))
    return "openai" if g and not y else "yandex" if y and not g else None


def light_quick(text):
    """Простые команды подсветке без модели: она путала состояние ленты и отвечала «уже горит», не вызывая инструмент."""
    t = text.lower()
    if SKY_BACK.search(t): return {"action": "sky_release"}, "Отдаю Скайдимо обратно, шеф."
    if SKY_TAKE.search(t): return {"action": "sky_take"}, "Беру подсветку монитора себе, секунду."
    mon = bool(re.search(r"монитор", t))  # «подсветку монитора красным»: колонка сама возьмёт ленту монитора у Skydimo
    if LQ_OFF.search(t): return {"action": "off", "monitor": mon}, random.choice(["Гашу.", "Погасил, братан.", "Всё, темнота."])
    rgb = color_rgb(re.sub(LQ_ANY, "", t)) if LQ_ANY.search(t) and re.search(r"\b(сделай|поставь|покрась|включи|зажги|поменяй|сменим|смени|давай)\b", t) else None
    if rgb: return {"action": "color", "rgb": rgb, "monitor": mon}, random.choice(["Готово.", "Красим, шеф.", "Держи цвет."])
    if LQ_ON.search(t): return {"action": "on", "monitor": mon}, random.choice(["Врубаю.", "Включил, шеф.", "Свет пошёл."])


PEND = {}  # device -> команда музыке для колонки (отдаётся через /followup)
# ponytail: STT иногда слышит «Скат» как «скот»/«с кат»; расширять по логам, если промахивается
WAKE = re.compile(r"^\W*(?:(?:эй|ну|слушай|привет|окей|ок)\W+)?(?:ск[аоэ]т[аы]?|с\s?кат)\b\W*", re.I)
JUNK = re.compile(r"^\W*(?:ска|кат|ат|ск)\b\W*", re.I)
pool = ThreadPoolExecutor(4)
SENT = re.compile(r"(?<=[.!?…])\s+|\n+")
SENT_FIRST = re.compile(r"(?<=[.!?…,:;—])\s+|\n+")


def post(url, data, headers, timeout=25):
    return urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=timeout)


def stt(pcm):
    r = post("https://stt.api.cloud.yandex.net/speech/v1/stt:recognize?lang=ru-RU&format=lpcm&sampleRateHertz=16000",
             pcm, {"Authorization": "Api-Key " + YA_KEY})
    return json.load(r).get("result", "").strip()


# эмоция -> (роль Yandex, множитель скорости, сдвиг тона); у Ермила роли только neutral и good
EMOS = {"joy": ("good", 1.08, 0), "laugh": ("good", 1.12, 60), "anger": ("neutral", 1.12, -100), "surprise": ("good", 1.1, 120),
        "irony": ("neutral", 0.95, -40), "sad": ("neutral", 0.88, -80), "tired": ("neutral", 0.85, -60)}
EMO_WORDS = (("joy", "радост|рад|весел|довол"), ("laugh", "смех|смеш|ржач|угар|хохот"), ("anger", "зло|гнев|ярост|бесит|раздраж"),
             ("surprise", "удив|шок|ого"), ("irony", "ирони|сарказ|подкол|насмеш|хитр"), ("sad", "грус|печал|тоск"),
             ("tired", "устал|сон|лень|скуч"))
TAG = re.compile(r"\s*\[([^\]\n]{2,20})\]\s*")


def emo_key(word):
    w = word.lower()
    return next((k for k, rx in EMO_WORDS if re.match(rx, w)), "calm")


EMO_INSTR = {"joy": "радостно, с улыбкой", "laugh": "смеясь, сквозь смех", "anger": "зло, жёстко, на повышенных тонах",
             "surprise": "удивлённо", "irony": "с иронией, насмешливо", "sad": "грустно, тише обычного", "tired": "устало, лениво"}


def oai_tts(text, emo=None):
    """Голос OpenAI (gpt-4o-mini-tts): сырой PCM 24 кГц моно, эмоция и характер задаются словами."""
    instr = ("Говори по-русски как дерзкий харизматичный дворовой кореш с чёрным юмором: живо, уверенно, чуть насмешливо, чёткая русская дикция. "
             + ("Сейчас: " + EMO_INSTR[emo] + "." if emo in EMO_INSTR else "Сейчас: уверенно и ровно."))
    body = {"model": "gpt-4o-mini-tts", "voice": OAI_VOICE, "input": text, "instructions": instr, "response_format": "pcm"}
    return post("https://api.openai.com/v1/audio/speech", json.dumps(body).encode(),
                {"Authorization": "Bearer " + OAI_KEY, "Content-Type": "application/json"}, 30).read()


def tts(text, emo=None):
    if ENGINE.get("v") == "openai" and OAI_KEY:
        try:
            pcm = oai_tts(text, emo)
            if len(pcm) > 2000: return pcm
        except Exception as e:
            print("openai tts error:", repr(e)[:100], "-> Yandex", flush=True)
    return tts_yandex(text, emo)


def tts_yandex(text, emo=None):
    """Yandex TTS v3 (REST, поток JSON со звуком base64): голос, роль, скорость и тон из окружения; v1 — запасной."""
    role, sp, pt = EMOS.get(emo, (ROLE, 1.0, 0))
    hints = [{"voice": VOICE}, {"speed": round(float(SPEED) * sp, 2)}] + ([{"role": role}] if role else []) + \
            ([{"pitchShift": float(PITCH) + pt}] if float(PITCH) + pt else [])
    body = {"text": text, "hints": hints, "loudnessNormalizationType": "LUFS",
            "outputAudioSpec": {"rawAudio": {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": 24000}}}
    try:
        pcm = b""
        for line in post("https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis", json.dumps(body).encode(),
                         {"Authorization": "Api-Key " + YA_KEY, "x-folder-id": FOLDER, "Content-Type": "application/json"}):
            if line.strip():
                chunk = json.loads(line).get("result", {}).get("audioChunk", {}).get("data")
                if chunk: pcm += base64.b64decode(chunk)
        if pcm: return pcm
    except Exception as e:
        print("tts v3 error", e, flush=True)
    d = urllib.parse.urlencode({"text": text, "lang": "ru-RU", "voice": "ermil", "speed": SPEED,
                                "format": "lpcm", "sampleRateHertz": 24000}).encode()
    return post("https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize", d,
                {"Authorization": "Api-Key " + YA_KEY}).read()


def clean(s):
    s = re.sub(r"\[служебно.*?\]", "", s, flags=re.S)  # страховка: служебные пометки вслух не произносим
    return re.sub(r"[*_`#>~|]|\[[^\]]*\]\([^)]*\)", "", s).strip()


TOOLS = [{"type": "function", "function": {
    "name": "web_search",
    "description": "Поиск актуальной информации в интернете: новости, погода, курсы валют и акций, спорт, события, "
                   "цены, расписания, свежие факты, всё, чего ты можешь не знать или что могло измениться.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "description":
                   "Короткий поисковый запрос (до 100 символов), лучше с городом и датой, если важно"}},
                   "required": ["query"]}}}]
TOOLS += [
    {"type": "function", "function": {
        "name": "draw_image", "description": "Нарисовать картинку по описанию и показать её на мониторе пользователя (занимает ~15 секунд).",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Подробное описание картинки (стиль, сюжет, цвета), можно по-русски"},
            "shape": {"type": "string", "enum": ["wide", "square", "tall"], "description": "формат: wide — горизонтальная, square, tall — вертикальная"}}, "required": ["prompt"]}}},
    {"type": "function", "function": {
        "name": "light_control", "description": "Подсветка в комнате (RGB-лента): включить, выключить, цвет, яркость.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["on", "off", "color", "brightness"]},
            "color": {"type": "string", "description": "Название цвета по-русски (красный, синий, тёплый белый…) или hex RRGGBB"},
            "brightness": {"type": "integer", "description": "Яркость 1-100 процентов"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "play_music", "description": "Включить музыку из Яндекс Музыки на колонке.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Исполнитель, трек, альбом или плейлист; пусто для «просто музыку»"},
            "kind": {"type": "string", "enum": ["auto", "wave", "likes", "station"],
                     "description": "auto — искать по запросу, wave — «Моя волна», likes — любимые треки пользователя, "
                                    "station — жанровая станция Яндекса по названию в query (рок, джаз, поп, медитация…)"},
            "mood": {"type": "string", "enum": ["active", "fun", "calm", "sad"], "description": "настроение «Моей волны», если просят"},
            "diversity": {"type": "string", "enum": ["favorite", "discover", "popular"],
                          "description": "«Моя волна»: любимое, незнакомое или популярное"}}}}},
    {"type": "function", "function": {
        "name": "music_control", "description": "Управление воспроизведением и громкостью колонки.",
        "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": [
            "stop", "pause", "resume", "next", "previous", "louder", "quieter"]}}, "required": ["action"]}}}]
TOOLS.append({"type": "function", "function": {
    "name": "pc_control", "description": "Управление компьютером пользователя (только из белого списка).",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["open_app", "close_app", "open_url", "search", "media", "lock"]},
        "target": {"type": "string", "description":
                   "open_app/close_app: название ЛЮБОЙ установленной программы так, как сказал пользователь (хром, телеграм, дискорд, "
                   "обс, фотошоп, ворд, steam…); open_url: youtube.com, google.com, yandex.ru, github.com, wikipedia.org, "
                   "vk.com, kinopoisk.ru, twitch.tv, music.yandex.ru, habr.com; search: поисковый запрос; "
                   "media: playpause, next, prev, volup, voldown, mute; lock: пусто"}},
        "required": ["action"]}}})
TOOLS.append({"type": "function", "function": {
    "name": "look_at_screen", "description": "Посмотреть на экран компьютера пользователя (снимок монитора) и подсказать, что "
    "на нём и что делать. Вызывай, когда пользователь просит помочь с тем, что открыто на мониторе («что тут нажимать», «что это за окно»).",
    "parameters": {"type": "object", "properties": {
        "monitor": {"type": "integer", "enum": [0, 1, 2], "description":
                    "номер монитора, как в настройках Windows («первый» — 1, «второй» — 2); 0 — если сказано «основной» или «главный» "
                    "монитор или номер не назван"},
        "question": {"type": "string", "description": "Что нужно выяснить по экрану, как сказал пользователь"}}, "required": ["question"]}}})
DSH = {"Authorization": "Bearer " + (DS_KEY or ""), "Content-Type": "application/json"}
VISION_SYSTEM = ("Ты Скат, голосовой помощник. Перед тобой снимок монитора пользователя и его вопрос. Ответь по-русски, как для "
                 "голоса: 2-4 коротких предложения (до 50 слов) без markdown и списков. Скажи, что это за окно или панель и что именно нажать: "
                 "называй кнопки и пункты точно так, как они подписаны, и указывай, где они (вверху слева, справа, внизу). "
                 "Если не хватает знаний, чтобы ответить уверенно (незнакомая программа, непонятные параметры), вызови web_search "
                 "с названием программы и сутью вопроса и ответь по найденному. Если нужного не видно, честно скажи, чего не видно. Сейчас {now}.")


def draw_image(prompt, shape="wide"):
    """Картинка от OpenAI -> файл на сервере; колонка заберёт её и отдаст ПК (показать на мониторе). Возвращает id или None."""
    if not OAI_KEY: return None
    size = {"square": "1024x1024", "tall": "1024x1536"}.get(shape, "1536x1024")
    body = {"model": OAI_IMG, "prompt": prompt[:900], "size": size, "quality": E("OPENAI_IMAGE_QUALITY", "medium"), "n": 1}
    try:
        r = post("https://api.openai.com/v1/images/generations", json.dumps(body).encode(),
                 {"Authorization": "Bearer " + OAI_KEY, "Content-Type": "application/json"}, 150)
        png = base64.b64decode(json.load(r)["data"][0]["b64_json"])
    except Exception as e:
        print("image error:", repr(e)[:150], flush=True); return None
    os.makedirs(IMG_DIR, exist_ok=True)
    old = sorted(os.listdir(IMG_DIR))
    for f in old[:-15]: os.remove(os.path.join(IMG_DIR, f))
    iid = str(int(time.time() * 1000))
    open(os.path.join(IMG_DIR, iid + ".png"), "wb").write(png)
    print(f"image {iid}: {len(png) // 1024} КБ, {prompt[:80]!r}", flush=True)
    return iid


def run_tool(name, args, original, ctx):
    """Музыка играет на колонке (Яндекс Музыка из-за рубежа недоступна), сервер лишь передаёт ей команду через /followup."""
    if name == "play_music":
        ctx["music"] = {"action": "play", "query": args.get("query", ""), "kind": args.get("kind", "auto"),
                        "mood": args.get("mood"), "diversity": args.get("diversity")}
        return "Музыка включится сразу после твоей реплики. Коротко подтверди, что именно включаешь."
    if name == "pc_control":
        items = ctx.setdefault("pcitems", []); items.append({"do": args.get("action"), "target": args.get("target", "")})
        ctx["music"] = {"action": "pc", "items": items}  # можно несколько действий за одну реплику
        return "Команда передана компьютеру. Коротко подтверди, что делаешь."
    if name == "look_at_screen":
        ctx["music"] = {"action": "screen", "monitor": int(args.get("monitor") or 0), "question": args.get("question", "")}
        return "Снимок экрана будет сделан сразу после твоей реплики. Скажи лишь «Смотрю на экран», коротко."
    if name == "light_control":
        act, rgb = args.get("action", "on"), color_rgb(args.get("color"))
        ctx["light"] = {"action": act, "rgb": rgb, "gain": args.get("brightness")}
        if act == "color" and not rgb: ctx["light"] = None; return "Такой цвет я не знаю, переспроси."
        return "Сделано. Коротко подтверди."
    if name == "draw_image":
        ctx["image"] = draw_image(args.get("prompt", original), args.get("shape", "wide"))
        return "Картинка готова и сейчас появится на мониторе. Скажи об этом одной короткой фразой." if ctx["image"] else "Не получилось нарисовать, скажи об этом коротко."
    if name == "music_control":
        ctx["music"] = {"action": "ctl", "what": args.get("action", "stop")}
        return "Сделано."
    return web_search(args.get("query", original), original)


def web_search(query, original):
    """DeepSeek Search API -> текст для модели (заголовок, дата, сайт, краткое содержание)."""
    try:
        r = post("https://api.deepseek.com/v1/tools/search",
                 json.dumps({"query": query[:128], "count": 5, "ori_question": original[:128]}).encode(), DSH, 20)
        res = json.load(r).get("results", [])[:5]
    except Exception as e:
        print("search error", e, flush=True); return "Поиск сейчас недоступен."
    print(f"search {query!r}: {len(res)} results", flush=True)
    return "\n\n".join(f"{x.get('title', '')} ({(x.get('date') or '')[:10]}, {x.get('site_name') or ''}): "
                       f"{(x.get('summary') or x.get('snippet') or '')[:600]}" for x in res) or "Ничего не найдено."


def _chat_stream_one(prov, messages, tools, names, force):
    """Поток одной модели: ('text', кусок) или ('tool', {index: {id, name, args}}) в конце. Обе API в формате OpenAI."""
    if prov == "openai":
        url, key = "https://api.openai.com/v1/chat/completions", OAI_KEY
        body = {"model": OAI_MODEL, "messages": messages, "stream": True, "max_completion_tokens": 600}
    else:
        url, key = "https://api.deepseek.com/chat/completions", DS_KEY
        body = {"model": MODEL, "messages": messages, "stream": True, "max_tokens": 600, "thinking": {"type": "disabled"}}
    hdr = {"Authorization": "Bearer " + (key or ""), "Content-Type": "application/json"}
    if tools: body["tools"] = [t for t in TOOLS if names is None or t["function"]["name"] in names]
    if tools and force: body["tool_choice"] = {"type": "function", "function": {"name": "web_search"}}
    calls = {}
    for line in post(url, json.dumps(body).encode(), hdr, 20):
        line = line.decode().strip()
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        d = json.loads(line[5:])["choices"][0]["delta"]
        if d.get("content"): yield "text", d["content"]
        for t in d.get("tool_calls") or []:
            c = calls.setdefault(t["index"], {"id": "", "name": "", "args": ""})
            c["id"] = t.get("id") or c["id"]
            f = t.get("function") or {}
            c["name"] += f.get("name") or ""; c["args"] += f.get("arguments") or ""
    if calls: yield "tool", calls


def chat_stream(messages, tools, names=None, force=False, prov="deepseek"):
    """Выбранная модель; если она не отвечает (сеть, лимит, ошибка), до первого слова тихо переходим на другую."""
    order = [prov] + [p for p in ("deepseek", "openai") if p != prov and (OAI_KEY if p == "openai" else DS_KEY)]
    for i, p in enumerate(order):
        started = False
        try:
            for ev in _chat_stream_one(p, messages, tools, names, force):
                started = True; yield ev
            return
        except Exception as e:
            print(f"llm {p} error: {e!r}", flush=True)
            if started or i == len(order) - 1: raise
            print(f"llm: перехожу на {order[i + 1]}", flush=True)


REFUSE = re.compile(r"не знаю|гадать не|не буду гадать|не уверен|нет (у меня )?(данных|информации)|не могу (сказать|знать|ответить)|не располагаю|не в курсе|затрудняюсь", re.I)


def llm_sentences(messages, original, ctx, names=None):
    """Генератор готовых к озвучке предложений. Модель может вызвать web_search (до 2 раз за ответ)."""
    spoken, force = False, False
    for turn in range(3):
        buf, calls, tagged = "", None, False
        for kind, val in chat_stream(messages, tools=turn < 2, names=names, force=force, prov=ctx.get("prov", "deepseek")):
            if kind == "tool": calls = val; continue
            buf += val
            if not tagged and buf.strip():  # тег эмоции в начале ответа: берём и не озвучиваем
                st = buf.lstrip()
                if st[0] == "[" and "]" not in st and len(st) < 24: continue
                m = TAG.match(buf)
                if m: ctx.setdefault("emo", emo_key(m.group(1))); buf = buf[m.end():]
                tagged = True
            if turn == 0 and not spoken and not force and REFUSE.search(buf) and (names is None or "web_search" in names):
                force = True; break  # модель отнекивается вместо поиска: гасим ответ и повторяем с обязательным поиском
            parts = (SENT if spoken else SENT_FIRST).split(buf)  # первую фразу режем и по запятой — звук раньше
            # слишком короткие куски склеиваем со следующим, иначе речь рубленая
            while len(parts) > 1:
                s = clean(parts[0])
                if len(s) < (12 if spoken else 18):  # короткий кусок: клеим со следующим или ждём продолжения
                    parts[1] = parts[0] + " " + parts[1]; parts.pop(0); continue
                if s: yield s; spoken = True
                parts.pop(0)
            buf = parts[0]
        if force and turn == 0 and not calls:
            continue
        if clean(buf):
            yield clean(buf)
        if not calls: return
        if turn == 0 and not clean(buf) and any(c["name"] == "web_search" for c in calls.values()):
            yield "Секунду, посмотрю в интернете."  # чтобы не молчать, пока ищем
        if turn == 0 and not clean(buf) and any(c["name"] == "draw_image" for c in calls.values()):
            yield "Рисую, шеф, это секунд двадцать."
        messages = messages + [{"role": "assistant", "content": buf or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
            for c in calls.values()]}]
        for c in calls.values():
            try: args = json.loads(c["args"])
            except ValueError: args = {}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": run_tool(c["name"], args, original, ctx)})


MUSIC_ON = {}  # device -> играет ли музыка (по командам play_music/stop)
APPS = {"chrome": ("хром|chrome|браузер", "хром"), "telegram": ("телеграм\\w*|telegram|телег\\w+", "телеграм"),
        "vscode": ("vs ?code|вс ?код", "вс код"), "cursor": ("курсор|cursor", "курсор"), "discord": ("дискорд\\w*", "дискорд"),
        "steam": ("стим", "стим"), "notepad": ("блокнот\\w*", "блокнот"), "calc": ("калькулятор\\w*", "калькулятор"),
        "explorer": ("проводник\\w*", "проводник")}
SITES = {"youtube.com": "ютуб|youtube", "github.com": "гитхаб|github", "wikipedia.org": "википеди\\w+", "vk.com": "вконтакте|вк",
         "kinopoisk.ru": "кинопоиск\\w*", "twitch.tv": "твич|twitch", "habr.com": "хабр\\w*"}
RULES = [  # (что, регэксп, ответ, нужна ли играющая музыка)
    ("stop", r"\b(стоп|хватит|выключи (музыку|песню|радио)|останови( музыку| песню)?)\b", "Хорошо, выключаю.", True),
    ("pause", r"\b(пауз\w*|приостанови\w*)\b", "Пауза.", True),
    ("resume", r"\b(продолж\w*|сними с паузы|верни музыку|включи снова)\b", "Продолжаю.", True),
    ("next", r"\b(следующ\w*|дальше|пропусти\w*|другую (песню|песня|трек))\b", "Дальше.", True),
    ("previous", r"\b(предыдущ\w*|назад|прошлый трек|прошлую песню)\b", "Назад.", True),
    ("dislike", r"\b(дизлайк|не нравится|не люблю|надоело)\b", "Понял, пропускаю.", True),
    ("like", r"\b(лайк|мне нравится|нравится)\b", "Запомнил.", True),
    ("louder", r"\b(громче|погромче|прибавь( громкость)?|увеличь громкость)\b", "Громче.", False),
    ("quieter", r"\b(тише|потише|убавь( громкость)?|уменьши громкость)\b", "Тише.", False)]


def pc_quick(t):
    """«Открой хром», «закрой телеграм», «открой ютуб», «заблокируй компьютер» без модели."""
    if re.search(r"\bзаблокируй (компьютер|экран|пк)\b", t):
        return {"action": "pc", "items": [{"do": "lock", "target": ""}]}, "Блокирую."
    opening = re.search(r"\b(открой|запусти|включи)\b", t); closing = re.search(r"\b(закрой)\b", t)
    if not (opening or closing): return None
    items, names = [], []
    generic = re.fullmatch(r"(открой|запусти|закрой)( программу| приложение)? (.+)", t)  # любая программа по названию
    if generic and not re.search(r"\b(музык\w*|волн\w*|песн\w*|трек\w*|свет|дверь|окно|мне|для|что|как|почему)\b", generic.group(3)):
        verb = "close_app" if generic.group(1) == "закрой" else "open_app"
        parts = [x.strip() for x in re.split(r"\s+и\s+|,", generic.group(3)) if x.strip()]
        if 1 <= len(parts) <= 5 and all(len(p.split()) <= 3 for p in parts):
            known = [dom for dom, rx in SITES.items() if any(re.search(rf"\b({rx})\b", p) for p in parts)]
            if not known:  # сайты (ютуб и т.п.) обрабатываются ниже
                return {"action": "pc", "items": [{"do": verb, "target": p} for p in parts]}, \
                    ("Закрываю " if verb == "close_app" else "Открываю ") + " и ".join(parts) + "."
    for key, (rx, name) in APPS.items():
        if re.search(rf"\b({rx})\b", t):
            items.append({"do": "open_app" if opening else "close_app", "target": key}); names.append(name)
    if opening:
        for dom, rx in SITES.items():
            if re.search(rf"\b({rx})\b", t): items.append({"do": "open_url", "target": dom}); names.append(dom.split(".")[0])
    if not items: return None
    return {"action": "pc", "items": items}, ("Открываю " if opening else "Закрываю ") + " и ".join(names) + "."


LIVE_RX = re.compile(r"(живой|живое|живую)\s+(разговор|общени|диалог|беседу)|режим\s+(разговор|диалог|беседы)|давай\s+(поговорим|поболтаем|пообщаемся|побеседуем)", re.I)


def quick_command(text, dev):
    """Короткие команды (до 6 слов) разбираем правилами. Управление треками только при играющей музыке."""
    t = text.lower().strip(" .,!?")
    if len(t.split()) > 8: return None
    if LIVE_RX.search(t) and OAI_KEY: return {"action": "live", "on": True}, "Давай, шеф. Говори, я слушаю."
    if re.search(r"(звук|аудио)\w*\s+(?:с\s+)?(компьютер|пк|комп)\w*|колонк\w*\s+(для|от)\s+(компьютер|пк|комп)\w*|(компьютер|пк|комп)\w*\s+(на|через)\s+колонк\w*|трансляц\w*.*(компьютер|пк|комп)|транслир\w*.*(звук|компьютер|пк|комп)", t)             and not re.search(r"(тише|громче|громкост|без звука)", t)             or (len(t.split()) <= 4 and re.search(r"\b(с|от|из)\s+(компьютер|пк|комп)\w*$", t)  # распознавание часто роняет «звук»: «с компьютера»
                and not re.search(r"(открой|закрой|запусти|заблок|найди|покажи|посмотри)", t)):
        if re.search(r"\b(выключи|отключи|останови|хватит|убери|верни|прекрати|стоп)\w*", t):
            return {"action": "pcaudio", "on": False}, "Отключаю звук компьютера."
        MUSIC_ON[dev] = True  # без глагола («звук с компьютера», «трансляция звука») — тоже включить: распознавание часто теряет «включи»
        return {"action": "pcaudio", "on": True}, "Выводю звук компьютера на колонку."
    pc = pc_quick(t)
    if pc: return pc
    if re.search(r"\bволн\w*", t) and re.search(r"\b(включи|поставь|давай|запусти|хочу|мою|свою)\b", t):  # «Моя волна»
        mood = next((v for rx, v in (("бодр|энерг|актив", "active"), ("весел|позитив", "fun"), ("спокой|расслаб|медит", "calm"),
                                      ("груст|печал", "sad")) if re.search(rx, t)), None)
        div = next((v for rx, v in (("нов|незнаком|открой", "discover"), ("любим|знаком", "favorite"), ("популяр", "popular"))
                    if re.search(rx, t)), None)
        MUSIC_ON[dev] = True
        return {"action": "play", "query": "", "kind": "wave", "mood": mood, "diversity": div}, "Включаю Мою волну."
    if len(t.split()) > 6: return None
    for what, rx, reply, needs_music in RULES:
        if re.search(rx, t) and (MUSIC_ON.get(dev) or not needs_music):
            if what == "stop": MUSIC_ON[dev] = False
            return {"action": "ctl", "what": what}, reply
    return None


RT_MODEL, RT_VOICE = E("OPENAI_RT_MODEL", "gpt-realtime-2.1-mini"), E("OPENAI_RT_VOICE", "cedar")
LIVE = {}  # sid -> Live


class Live:
    """Живой разговор: колонка шлёт звук микрофона (PCM 24 кГц), мы пересылаем в OpenAI Realtime и отдаём голос обратно кадрами
    [тип 1 байт][длина 4 байта][данные]: 0 — звук, 1 — конец, 2 — человек заговорил (прервать), 3 — реплика закончилась."""

    def __init__(self, dev):
        import wsclient
        self.dev, self.q, self.closed, self.activity = dev, queue.Queue(), False, time.time()
        self.ws = wsclient.WS("wss://api.openai.com/v1/realtime?model=" + RT_MODEL, ["Authorization: Bearer " + OAI_KEY])
        self.ws.recv()  # session.created
        d = datetime.now(ZoneInfo(E("SKAT_TZ", "Europe/Moscow")))
        instr = (PERSONA + "В этом разговоре теги эмоций в квадратных скобках НЕ пиши и не произноси: эмоцию передавай голосом и интонацией. " + f"Это разговор голосом вживую, сейчас {d:%d.%m.%Y %H:%M} по Москве. Отвечай по-русски, коротко (одно-три предложения), живо, "
                 "без списков. Если пользователь хочет закончить разговор (хватит, пока, закончим, всё, спасибо, свободен), вызови end_conversation "
                 "и попрощайся одной фразой. За свежими данными (погода, новости, курсы, события) вызывай web_search. ")
        self.ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "output_modalities": ["audio"], "instructions": instr,
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                "turn_detection": {"type": "server_vad", "silence_duration_ms": 650, "create_response": True, "interrupt_response": True},
                                "transcription": {"model": "gpt-4o-mini-transcribe", "language": "ru"}},
                      "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": RT_VOICE}},
            "tools": [{"type": "function", "name": "web_search", "description": "Поиск свежей информации в интернете",
                       "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
                      {"type": "function", "name": "end_conversation", "description": "Закончить голосовой разговор, когда пользователь попрощался",
                       "parameters": {"type": "object", "properties": {}}}]}}))
        threading.Thread(target=self.read, daemon=True).start()
        threading.Thread(target=self.watch, daemon=True).start()

    def append(self, pcm):
        if not self.closed: self.ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))

    def read(self):
        try:
            while not self.closed:
                m = json.loads(self.ws.recv()); t = m.get("type")
                if t in ("response.output_audio.delta", "response.audio.delta"): self.q.put((0, base64.b64decode(m["delta"]))); self.activity = time.time()
                elif t == "input_audio_buffer.speech_started": self.q.put((2, b"")); self.activity = time.time()
                elif t == "response.done": self.q.put((3, b""))
                elif t == "conversation.item.input_audio_transcription.completed": print(f"[live] ты: {m.get('transcript', '').strip()!r}", flush=True)
                elif t == "response.output_audio_transcript.done": print(f"[live] Скат: {m.get('transcript', '')!r}", flush=True)
                elif t == "response.function_call_arguments.done": threading.Thread(target=self.tool, args=(m,), daemon=True).start()
                elif t == "error": print("[live] error:", str(m.get("error"))[:200], flush=True)
        except Exception as e:
            print("[live] read:", repr(e)[:100], flush=True)
        self.finish()

    def tool(self, m):
        name, call = m.get("name"), m.get("call_id")
        try: args = json.loads(m.get("arguments") or "{}")
        except ValueError: args = {}
        if name == "end_conversation":
            out = "ok"; threading.Timer(5.0, self.finish).start()  # прощание успеет договориться
        elif name == "web_search":
            out = web_search(args.get("query", ""), args.get("query", ""))
        else: out = "такого инструмента нет"
        try:
            self.ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call, "output": out}}))
            self.ws.send(json.dumps({"type": "response.create"}))
        except Exception: pass

    def watch(self):
        while not self.closed:
            time.sleep(5)
            if time.time() - self.activity > 150: print("[live] тишина, закрываю", flush=True); self.finish()

    def finish(self):
        if self.closed: return
        self.closed = True
        try: self.ws.close()
        except Exception: pass
        self.q.put((1, b"")); LIVE.pop(self.sid, None) if hasattr(self, "sid") else None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def reply(self, code, body=b""):
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def chunk(self, b):
        if b:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b)); self.wfile.flush()

    def start_audio(self, kind="answer", emo="calm", light=None):
        self.send_response(200); self.send_header("Content-Type", "audio/L16"); self.send_header("X-Kind", kind); self.send_header("X-Emo", emo)
        if light: self.send_header("X-Light", json.dumps(light))  # команда ленте применяется сразу, а не после реплики
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()

    def end_audio(self):
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()

    def live_chunks(self):
        """Тело запроса с Transfer-Encoding: chunked (BaseHTTPRequestHandler его сам не разбирает)."""
        while True:
            line = self.rfile.readline()
            if not line: return
            n = int(line.split(b";")[0].strip() or b"0", 16)
            if n == 0: self.rfile.readline(); return
            data = self.rfile.read(n); self.rfile.readline()
            yield data

    def do_GET(self):
        if self.path.startswith("/live/down") and self.headers.get("X-Token") == TOKEN:  # голос из OpenAI -> колонке
            s = LIVE.get(self.path.split("sid=")[-1])
            if not s: return self.reply(404)
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            try:
                while True:
                    try: typ, data = s.q.get(timeout=1.0)
                    except queue.Empty:
                        if not s.closed: continue
                        typ, data = 1, b""
                    self.chunk(bytes([typ]) + struct.pack(">I", len(data)) + data)
                    if typ == 1: break
                self.end_audio()
            except OSError: s.finish()
            return
        if self.path.startswith("/img/") and self.headers.get("X-Token") == TOKEN:  # забрать нарисованную картинку
            f = os.path.join(IMG_DIR, os.path.basename(self.path[5:]) + ".png")
            if not os.path.exists(f): return self.reply(404)
            b = open(f, "rb").read(); self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        self.reply(200, b"ok") if self.path == "/health" else self.reply(404)

    def do_POST(self):
        if self.path.startswith("/live/") and self.headers.get("X-Token") == TOKEN:
            if self.path == "/live/start":
                if not OAI_KEY: return self.reply(503)
                try: s = Live(self.headers.get("X-Device", "default"))
                except Exception as e: print("[live] start error:", repr(e)[:150], flush=True); return self.reply(502)
                s.sid = str(int(time.time() * 1000)); LIVE[s.sid] = s
                print(f"[live] начат {s.sid}, модель {RT_MODEL}", flush=True)
                return self.reply(200, json.dumps({"sid": s.sid}).encode())
            s = LIVE.get(self.path.split("sid=")[-1])
            if not s: return self.reply(404)
            if self.path.startswith("/live/stop"): s.finish(); return self.reply(200)
            if self.path.startswith("/live/up"):  # микрофон колонки (PCM 24 кГц) -> OpenAI
                try:
                    for chunk in self.live_chunks(): s.append(chunk)
                except Exception as e: print("[live] up:", repr(e)[:100], flush=True)
                return self.reply(200)
            return self.reply(404)
        if self.path not in ("/ask", "/followup", "/say", "/verify", "/vision") or self.headers.get("X-Token") != TOKEN:
            return self.reply(403)
        if self.path == "/vision":  # снимок монитора с ПК + вопрос -> DeepSeek с картинкой (и при нужде поиск) -> голос
            n = min(int(self.headers.get("Content-Length", 0)), 4_000_000)
            jpg, q = self.rfile.read(n), urllib.parse.unquote(self.headers.get("X-Question", "")) or "Что на экране и что мне делать?"
            d = datetime.now(ZoneInfo(E("SKAT_TZ", "Europe/Moscow")))
            msgs = [{"role": "system", "content": VISION_SYSTEM.format(now=f"{d:%d.%m.%Y %H:%M} по Москве")},
                    {"role": "user", "content": [{"type": "text", "text": q}, {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}]}]
            print(f"[vision] снимок {len(jpg)//1024} КБ, вопрос {q!r}", flush=True)
            qq, said, t0 = queue.Queue(), [], time.time()

            def prod():
                try:
                    for s in llm_sentences(msgs, q, {"music": None, "prov": PROVIDER.get(self.headers.get("X-Device", "default"), "deepseek")}, names=("web_search",)):
                        said.append(s); qq.put(pool.submit(tts, s))
                except Exception as e:
                    print("vision error", e, flush=True)
                    if not said: qq.put(pool.submit(tts, "Не получилось разобрать экран."))
                qq.put(None)
            threading.Thread(target=prod, daemon=True).start()
            self.start_audio("vision")
            try:
                while (f := qq.get()) is not None: self.chunk(f.result())
                self.end_audio()
            except Exception as e: print("vision send error", e, flush=True)
            print(f"[vision] сказал {' '.join(said)!r} за {time.time()-t0:.1f}с", flush=True)
            return
        if self.path == "/verify":  # колонка при музыке: «в очищенном звуке был всплеск голоса — это «Скат»?»
            n = min(int(self.headers.get("Content-Length", 0)), 200_000)
            try: text = stt(self.rfile.read(n))
            except Exception as e: print("verify error", e, flush=True); return self.reply(502)
            ok = bool(re.search(r"\b(ск[аоэ]т\w{0,2}|скад\w*)\b", text.lower())) or (  # проверка пробуждения принимает и обрывок слова: детектор срабатывает до конца «т»
                bool(self.headers.get("X-Loose")) and bool(re.search(r"\bск[аоэ]|\bск[ао]д|скат", text.lower())))
            print(f"[{self.headers.get('X-Device', '?')}] verify {text!r} ok={ok}", flush=True)
            return self.reply(200, json.dumps({"ok": ok, "text": text}, ensure_ascii=False).encode())
        if self.path == "/say":  # озвучить короткую фразу (колонка говорит от себя, например «не нашёл»)
            n = min(int(self.headers.get("Content-Length", 0)), 500)
            self.start_audio("say"); self.chunk(tts(self.rfile.read(n).decode("utf-8")[:300])); self.end_audio()
            return
        if self.path == "/followup":  # колонка договорила: если ответ был вопросом, ещё LISTEN_SEC слушаем без «Скат»
            dev = self.headers.get("X-Device", "default")
            asked = LASTQ.pop(dev, False)
            if asked: LISTEN[dev] = time.time() + LISTEN_SEC
            return self.reply(200, json.dumps({"q": asked, "music": PEND.pop(dev, None), "light": LPEND.pop(dev, None), "image": IPEND.pop(dev, None)}, ensure_ascii=False).encode())
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            return self.reply(413)
        data, t0 = self.rfile.read(n), time.time()
        dev = self.headers.get("X-Device", "default")
        try:
            text = data.decode("utf-8") if self.headers.get("Content-Type", "").startswith("text/") else stt(data)
        except Exception as e:
            print("input error", e, flush=True); return self.reply(502)
        # с X-Force «Скат» уже поймала колонка, но в начало фразы мог попасть его хвост («ат, какая погода»)
        m = WAKE.match(text) or (JUNK.match(text) if self.headers.get("X-Force") else None)
        listening = time.time() < LISTEN.get(dev, 0)
        if E("SKAT_DEBUG"): print(f"[{dev}] debug stt: {text!r}", flush=True)
        if not text or not (m or listening or self.headers.get("X-Force")):  # X-Force: нажали кнопку
            return self.reply(204)  # чужой разговор: молча игнорируем и не логируем
        if m:
            text = text[m.end():].strip()
        print(f"[{dev}] heard {text!r} ({time.time()-t0:.2f}s)", flush=True)
        if not text:  # одно «Скат»: без звука, колонка сама даёт сигнал, и ждём команду
            LISTEN[dev] = time.time() + LISTEN_SEC
            self.start_audio("ack"); self.end_audio()
            return
        LISTEN.pop(dev, None)
        if "X-Music" in self.headers: MUSIC_ON[dev] = True  # колонка сообщила: музыка играет (после рестарта сервера тоже)
        es = engine_switch(text)
        if es:
            if es == "openai" and not OAI_KEY: reply = "Ключа OpenAI у меня нет."
            else:
                ENGINE["v"] = es
                try: json.dump(ENGINE, open(ENG_FILE, "w"))
                except OSError: pass
                reply = "Теперь говорю голосом OpenAI, шеф." if es == "openai" else "Вернул голос Яндекса."
            print(f"[{dev}] голос: {ENGINE['v']}", flush=True); LASTQ[dev] = False
            self.start_audio(); self.chunk(tts(reply)); self.end_audio()
            return
        sw = provider_switch(text)
        if sw:
            if sw == "openai" and not OAI_KEY: reply = "Ключа ChatGPT у меня нет."
            else:
                PROVIDER[dev] = sw; reply = f"Теперь отвечает {PROV_NAMES[sw]}, шеф."
                try: json.dump(PROVIDER, open(PROV_FILE, "w"))
                except OSError: pass
            print(f"[{dev}] модель: {PROVIDER.get(dev, 'deepseek')}", flush=True); LASTQ[dev] = False
            self.start_audio(); self.chunk(tts(reply)); self.end_audio()
            return
        quick = quick_command(text, dev)
        if quick:  # простые команды без модели: быстрее и надёжнее
            pend, reply = quick
            PEND[dev] = pend; LASTQ[dev] = False
            print(f"[{dev}] quick: {pend}", flush=True)
            self.start_audio(); self.chunk(tts(reply)); self.end_audio()
            return
        lq = light_quick(text)
        if lq:
            lq, reply = lq; LASTQ[dev] = False
            print(f"[{dev}] light quick: {lq}", flush=True)
            self.start_audio(light=lq); self.chunk(tts(reply)); self.end_audio()
            return
        ts, hist = HIST.get(dev, (0, []))
        if time.time() - ts > HIST_TTL:
            hist = []
        hist = hist + [{"role": "user", "content": text}]
        d = datetime.now(ZoneInfo(E("SKAT_TZ", "Europe/Moscow")))  # сервер в UTC, колонка в Москве
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        system = SYSTEM.format(now=f"{days[d.weekday()]}, {d:%d.%m.%Y}, {d:%H:%M} по Москве")
        q, answer, ctx = queue.Queue(), [], {"music": None, "prov": PROVIDER.get(dev, "deepseek")}

        def producer():
            try:
                for s in llm_sentences([{"role": "system", "content": system}] + hist[-12:], text, ctx):
                    answer.append(s); q.put(pool.submit(tts, s, ctx.get("emo")))
            except Exception as e:
                print("llm error", e, flush=True)
                if not answer: q.put(pool.submit(tts, "Извини, у меня сбой связи."))
            q.put(None)
        threading.Thread(target=producer, daemon=True).start()

        f = q.get()  # заголовок с эмоцией отправляем, когда готов первый звук (тег эмоции уже разобран)
        self.start_audio(emo=ctx.get("emo", "calm"))
        first = True
        try:
            while f is not None:
                self.chunk(f.result())
                if first:
                    print(f"[{dev}] first audio {time.time()-t0:.2f}s emo={ctx.get('emo', 'calm')}", flush=True); first = False
                f = q.get()
            self.end_audio()
        except Exception as e:
            print("send error", e, flush=True)
        LASTQ[dev] = bool(answer) and answer[-1].rstrip().endswith("?")
        if ctx.get("light"): LPEND[dev] = ctx["light"]
        if ctx.get("image"): IPEND[dev] = ctx["image"]
        if ctx["music"]:
            PEND[dev] = ctx["music"]
            if ctx["music"]["action"] == "play": MUSIC_ON[dev] = True
        if answer:
            HIST[dev] = (time.time(), (hist + [{"role": "assistant", "content": " ".join(answer)}])[-12:])
        print(f"[{dev}] said {' '.join(answer)!r} total {time.time()-t0:.2f}s", flush=True)


if __name__ == "__main__":
    assert YA_KEY and DS_KEY and TOKEN, "нужны YANDEX_API_KEY, DEEPSEEK_API_KEY, SKAT_TOKEN"
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/opt/skat/cert.pem", "/opt/skat/key.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()
