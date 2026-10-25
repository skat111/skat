"""Открытая русская речь (Golos crowd, SOVA RuDevices; CC-BY / открытые лицензии) -> потоки int16 16 кГц для обучения.
open_train.raw  — речь на обучение (негативы), open_test.raw — речь на проверку (другие дикторы, честная оценка ложных срабатываний),
open_conf.raw   — фразы с «похожими» словами (трудные негативы), open_skat.raw — фразы, где есть слово «скат» (если есть).
Между репликами 0,3 с тишины. Читаем порциями (на сервере мало памяти). Запуск: python prep_open.py"""
import io, random, re, wave
import numpy as np
import pyarrow.parquet as pq

random.seed(0)
SR = 16000
CONF = re.compile(r"\b(скот\w*|ска[а-я]*т\w*|кат\w*|каток|скач\w+|скаж\w+|скал\w+|скан\w+|скейт\w*|шкат\w*|кот\w*)", re.I)
EXACT = re.compile(r"\bскат(а|у|ом|е|ы|ов)?\b", re.I)
LIM = {"train": 2 * 3600, "test": 3600}
bins = {"train": [], "test": [], "conf": [], "exact": []}
WORDS = {"dalshe": re.compile(r"дальш\w*", re.I), "nazad": re.compile(r"назад", re.I), "pauza": re.compile(r"пауз\w*", re.I)}
words = {k: [] for k in WORDS}  # короткие реплики (до 3 слов) с командными словами: настоящие голоса для обучения
tot = {"train": 0.0, "test": 0.0, "conf": 0.0, "exact": 0.0}
gap = np.zeros(int(0.3 * SR), np.int16)


def pcm(b):
    try:
        w = wave.open(io.BytesIO(b))
        if w.getframerate() != SR or w.getnchannels() != 1: return None
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)
    except Exception: return None


def take(where, x):
    bins[where] += [x, gap]; tot[where] += len(x) / SR + 0.3


for path, role in (("data/golos_train0.parquet", "train"), ("data/sova_train0.parquet", "train"), ("data/golos_test0.parquet", "test")):
    pf = pq.ParquetFile(path); n = 0
    for batch in pf.iter_batches(batch_size=100):
        for r in batch.to_pylist():
            text = r["transcription"] or ""; n += 1
            if len(text.split()) <= 3:
                for k, rx in WORDS.items():
                    if rx.search(text):
                        x = pcm(r["audio"]["bytes"])
                        if x is not None: words[k].append(x)
            kind = "exact" if EXACT.search(text) else "conf" if CONF.search(text) else role
            if kind in LIM and (tot[kind] > LIM[kind] or random.random() > 0.55): continue  # прореживаем, чтобы влезть в лимит и память
            x = pcm(r["audio"]["bytes"])
            if x is not None:
                take(kind, x)
                if kind == "exact": print("«скат»:", text, flush=True)
    print(path, "строк:", n, "| train", round(tot["train"] / 60), "мин, test", round(tot["test"] / 60), "мин, conf", round(tot["conf"] / 60, 1),
          "мин, exact", round(tot["exact"], 1), "с", flush=True)
np.savez("open_words.npz", **{k: np.array(v, dtype=object) for k, v in words.items()})
print("командные слова (реплики до 3 слов):", {k: len(v) for k, v in words.items()}, flush=True)
for name in bins:
    if bins[name]:
        np.concatenate(bins[name]).tofile(f"open_{name}.raw"); print(f"open_{name}.raw: {tot[name]/60:.1f} мин", flush=True)
