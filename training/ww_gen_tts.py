"""Синтез обучающих клипов Yandex TTS (16 кГц): слово «Скат» разными голосами/скоростями и фразы-негативы.
Результат: tts.npz (pos_*, neg_*). Запуск: set -a; . /etc/skat.env; set +a; python gen_tts.py"""
import os, sys, time, urllib.error, urllib.parse, urllib.request
import numpy as np

H = {"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"]}
VOICES = ["alena", "filipp", "ermil", "jane", "madirus", "omazh", "zahar"]
EMO = {"alena": ["neutral", "good"], "jane": ["neutral", "good", "evil"], "omazh": ["neutral", "evil"], "ermil": ["neutral", "good"]}


def tts(text, voice, speed=1.0, emotion=None):
    p = {"text": text, "lang": "ru-RU", "voice": voice, "speed": str(speed), "format": "lpcm", "sampleRateHertz": 16000}
    if emotion: p["emotion"] = emotion
    for _ in range(3):
        try:
            return np.frombuffer(urllib.request.urlopen(urllib.request.Request(
                "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize", urllib.parse.urlencode(p).encode(), H), timeout=20).read(), np.int16)
        except urllib.error.HTTPError as e:
            if e.code == 400: return None
            time.sleep(1)
        except Exception: time.sleep(1)
    return None


def trim(x, thr=0.02):
    if x is None or len(x) == 0: return None
    a = np.abs(x.astype(np.float32)) / 32768
    idx = np.where(a > thr)[0]
    return x[max(0, idx[0] - 800): idx[-1] + 800] if len(idx) else None


POS_TEXT = ["Скат", "Скат!", "Скат.", "Скат?", "скат"]
NEG_TEXT = """Привет
Как дела
Который час
Включи свет
Выключи свет
Расскажи шутку
Какая сегодня погода
Стоп
Старт
Хватит
Алиса
Маруся
Окей гугл
Слушай
Скажи
Скажи пожалуйста
Скатерть
Скатерть на столе
Каток
Пойдём на каток
Скот
Скотч
Скала
Сказка
Расскажи сказку
Скачать
Скачай файл
Скоро
Скорость
Скрипка
Скрепка
Кот
Кошка
Кит
Квадрат
Ската
Скатав
Скатился
Ска
Сквозняк
Сказал
Он сказал
Сколько времени
Сколько стоит
Сколько будет два плюс два
Поставь будильник
Поставь таймер
Включи музыку
Сделай потише
Сделай громче
Что ты умеешь
Как тебя зовут
Спокойной ночи
Доброе утро
Добрый вечер
Спасибо
Пожалуйста
Да
Нет
Не знаю
Может быть
Ладно
Хорошо
Подожди
Давай
Сегодня хорошая погода и я иду гулять
Завтра будет дождь поэтому возьми зонт
Мне нужно купить хлеб молоко и яйца
Позвони маме и скажи что я задержусь
Какой сегодня день недели
Включи новости
Найди рецепт борща
Переведи это слово на английский
Что такое квантовая физика
Кто написал войну и мир
Сколько километров до луны
Сыграй мою любимую песню
Напомни мне позвонить врачу
Зачем ты это сделал
Я уже иду домой
Посмотри что там происходит
Закрой окно
Открой дверь
Принеси мне воды
Это очень интересно
Вот такие дела
Ну что поехали
Раз два три четыре пять
Шесть семь восемь девять десять
Школа
Страна
Стол
Стул
Скейт
Скайп
Скорая
Скидка
Склад
Скажем так
Ага
Угу
Ого
Ух ты
Кто там
Алло
Эй
Слышишь
Иди сюда""".split("\n")

if __name__ == "__main__":
    pos, neg, meta = [], [], []
    for v in VOICES:
        for sp in (0.8, 0.95, 1.1, 1.3):
            for emo in EMO.get(v, [None]):
                for t in POS_TEXT[(len(pos) % 3):: 3]:  # по 2 варианта текста, чтобы не раздувать число запросов
                    x = trim(tts(t, v, sp, emo))
                    if x is not None: pos.append(x)
    print("positives", len(pos), flush=True)
    for i, t in enumerate(NEG_TEXT):
        for v in (VOICES[i % len(VOICES)], VOICES[(i * 3 + 1) % len(VOICES)]):
            x = trim(tts(t, v, [0.9, 1.0, 1.15][i % 3], None))
            if x is not None: neg.append(x)
    print("negatives", len(neg), flush=True)
    np.savez("tts.npz", pos=np.array(pos, dtype=object), neg=np.array(neg, dtype=object))
