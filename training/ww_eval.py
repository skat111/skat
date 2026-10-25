"""Оценка потоковой реализации (wake.py) на отложенных данных: пропуски слова и ложные срабатывания по порогам.
Запуск: python eval.py [каталог с моделями]"""
import sys, time
import numpy as np
from wake import Wake, STEP

D = sys.argv[1] if len(sys.argv) > 1 else "."
SR = 16000
skat, neg = np.fromfile("rec_skat.raw", np.int16), np.fromfile("rec_neg.raw", np.int16)
w = Wake(D)


def probs(x):
    w.reset(); out = []
    for i in range(0, len(x) - STEP + 1, STEP):
        p = w.feed(x[i: i + STEP]); out.append(0.0 if p is None else p)
    return np.array(out)


def find_peaks(x, start_s=30):
    e = np.sqrt(np.convolve((x.astype(np.float32) ** 2), np.ones(160) / 160, "same"))[::160]
    sm = np.convolve(e, np.ones(25) / 25, "same"); floor = np.percentile(sm, 25)
    pk = [i for i in range(start_s * 100, len(sm) - 100) if sm[i] > 2.0 * floor and sm[i] == sm[max(0, i - 60): i + 61].max()]
    out = []
    for p in pk:
        if not out or p - out[-1] > 100: out.append(p)
    return [p / 100 for p in out]


peaks = find_peaks(skat); test_words = peaks[4::5]
t = time.time(); pn = probs(neg[int(len(neg) * 0.7):]); dt = (time.time() - t) / len(pn)
noise_all = skat[8 * SR: 30 * SR]; pz = probs(noise_all[len(noise_all) // 2:])
print(f"шаг 80 мс считается за {dt*1000:.1f} мс (на сервере)")
mins = (len(pn) + len(pz)) * STEP / SR / 60
words = [probs(skat[int((p - 2.0) * SR): int((p + 1.5) * SR)]).max() for p in test_words]
allw = [probs(skat[int((p - 2.0) * SR): int((p + 1.5) * SR)]).max() for p in peaks]
print("слова (отложенные):", [round(float(v), 2) for v in words])
print(f"отложенный негатив {mins:.1f} мин; все {len(peaks)} слов записи для справки (часть из них учавствовала в обучении)")
print(" thr   TPR(отложенные)  TPR(все)  ложных/мин")
for thr in (0.3, 0.5, 0.7, 0.8, 0.9, 0.95):
    def trig(p):
        idx = np.where(p > thr)[0]; return 0 if not len(idx) else 1 + int(np.sum(np.diff(idx) > 19))
    print(f" {thr:.2f}   {np.mean([v > thr for v in words]):5.2f}            {np.mean([v > thr for v in allw]):5.2f}     {(trig(pn) + trig(pz)) / mins:5.2f}")
