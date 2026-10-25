"""Обучение детектора слова «Скат» поверх эмбеддингов openWakeWord (16 кадров x 96 на 2 с звука), версия 3.
Данные: ваши записи (rec_skat.raw — слово, rec_neg.raw — речь, bg.raw — 25 минут обычной жизни комнаты),
TTS (tts.npz, neg2.npz). Чтобы модель не училась на «подпись сессии»: шум для всех классов берётся из одного
большого банка bg.raw, а негативы берутся и из сессии со словом (вдали от слов).
Два раунда добычи трудных негативов, затем честная оценка на отложенных 20% bg.raw (ложных срабатываний в час).
Выход: skat_model.npz. Запуск: nice python train.py [seed]"""
import sys, time
import numpy as np
from openwakeword.utils import AudioFeatures
from sklearn.neural_network import MLPClassifier

SR, CLIP = 16000, 32000
rng = np.random.default_rng(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
skat, neg, bg = (np.fromfile(f, np.int16) for f in ("rec_skat.raw", "rec_neg.raw", "bg.raw"))
bg = bg[12 * SR:]  # первые секунды после старта arecord — перегруз
tts = np.load("tts.npz", allow_pickle=True)
TP, TN = list(tts["pos"]), list(tts["neg"])
TN2 = list(np.load("neg2.npz", allow_pickle=True)["neg"])
rng.shuffle(TN2); hold = int(len(TN2) * 0.15); TN2_te, TN2 = TN2[:hold], TN2[hold:]
F = AudioFeatures(inference_framework="onnx", ncpu=1)


def rms(x): return float(np.sqrt(np.mean(x.astype(np.float32) ** 2)) + 1e-6)


def find_peaks(x, start_s=30):
    e = np.sqrt(np.convolve((x.astype(np.float32) ** 2), np.ones(160) / 160, "same"))[::160]
    sm = np.convolve(e, np.ones(25) / 25, "same"); floor = np.percentile(sm, 25)
    pk = [i for i in range(start_s * 100, len(sm) - 100) if sm[i] > 2.0 * floor and sm[i] == sm[max(0, i - 60): i + 61].max()]
    out = []
    for p in pk:
        if not out or p - out[-1] > 100: out.append(p)
    return [p / 100 for p in out]


peaks = find_peaks(skat)
test_words = peaks[4::5]; train_words = [p for p in peaks if p not in test_words]
cutb, cutn = int(len(bg) * 0.8), int(len(neg) * 0.7)
bg_tr, bg_te, neg_tr, neg_te = bg[:cutb], bg[cutb:], neg[:cutn], neg[cutn:]
print(f"words {len(peaks)} (held {len(test_words)}); bg train {len(bg_tr)/SR/60:.1f} min, held {len(bg_te)/SR/60:.1f} min", flush=True)
# банк шума: тихие окна из bg_tr (разные состояния комнаты) + шум из сессии со словом
w = bg_tr[: len(bg_tr) // CLIP * CLIP].reshape(-1, CLIP)
quiet = w[np.argsort([rms(x) for x in w])[: max(20, len(w) // 3)]]
noise_bank = np.concatenate([quiet.reshape(-1), skat[8 * SR: 30 * SR]])
# окна сессии со словом вдали от слов (≥1.3 с) — негативы, чтобы «подпись сессии» не выдавала слово
far = [i for i in range(2 * SR, len(skat) - 2 * SR, SR // 4) if all(abs(i / SR - p) > 1.3 for p in peaks)]


def noise_clip(n=CLIP):
    i = rng.integers(0, len(noise_bank) - n); return noise_bank[i: i + n].astype(np.float32)


def put_word(word, end_off, snr):
    bg_ = noise_clip() * rng.uniform(0.7, 1.6)
    wd = word.astype(np.float32); wd *= (rms(bg_) * 10 ** (snr / 20) / rms(wd))
    end = CLIP - int(end_off * SR); start = end - len(wd)
    if start < 0: wd, start = wd[-start:], 0
    bg_[start: start + len(wd)] += wd[: CLIP - start]
    return bg_


def real_clip(p, end_off):
    end = int((p + 0.28 + end_off) * SR); x = skat[end - CLIP: end].astype(np.float32)
    return x * rng.uniform(0.5, 2.0) + noise_clip() * rng.uniform(0, 0.6)


tofix = lambda x: np.clip(x, -32768, 32767).astype(np.int16)
Es, ys, buf = [], [], []
def flush():
    global buf
    if buf: Es.append(F.embed_clips(np.array([b for b, _ in buf]), batch_size=64)); ys.extend(l for _, l in buf); buf = []
def add(c, lab):
    buf.append((tofix(c), lab))
    if len(buf) >= 800: flush()


t0 = time.time()
for wd in TP:
    for _ in range(10): add(put_word(wd, rng.uniform(0.1, 0.55), rng.uniform(1, 16)), 1)
for p in train_words:
    for _ in range(30): add(real_clip(p, rng.uniform(0.05, 0.55)), 1)
for wd in TN:
    for k in range(5): add(put_word(wd, rng.uniform(0.0, 0.5) if k < 3 else rng.uniform(0.3, 1.6), rng.uniform(1, 16)), 0)
for wd in TN2:
    add(put_word(wd, rng.uniform(0.0, 0.4), rng.uniform(1, 16)), 0)
    add(put_word(wd, rng.uniform(0.3, 1.6), rng.uniform(1, 16)), 0)
for wd in TP:
    for _ in range(3): add(put_word(wd[: int(len(wd) * rng.uniform(0.3, 0.6))], rng.uniform(0.0, 0.15), rng.uniform(1, 16)), 0)
for _ in range(2000):
    i = rng.integers(0, len(neg_tr) - CLIP); add(neg_tr[i: i + CLIP].astype(np.float32) * rng.uniform(0.5, 2.0), 0)
for _ in range(4000):
    i = rng.integers(0, len(bg_tr) - CLIP); add(bg_tr[i: i + CLIP].astype(np.float32) * rng.uniform(0.6, 2.5), 0)
for i in rng.choice(far, 1200): add(skat[i: i + CLIP].astype(np.float32) * rng.uniform(0.5, 2.0), 0)
for _ in range(600): add(noise_clip() * rng.uniform(0.3, 2.0), 0)
flush()
E = np.concatenate(Es); y = np.array(ys)
print("embeddings", E.shape, "pos", int(y.sum()), "neg", int((1 - y).sum()), f"{time.time()-t0:.0f}s", flush=True)
flat = lambda e: e.reshape(len(e), -1)
mean, std = flat(E).mean(0), flat(E).std(0) + 1e-6
Z, yy = (flat(E) - mean) / std, y.copy()
del E


def fit(Z, y):
    return MLPClassifier((128,), alpha=1e-3, max_iter=200, early_stopping=True, random_state=0, n_iter_no_change=8).fit(Z, y)


def stream_probs(clf, x):
    wins = []
    for s in range(0, len(x), 40 * SR):  # библиотека ломается на длинных кусках; склеиваем окна по 40 с
        seg = x[s: s + 40 * SR]
        if len(seg) < 3 * SR: continue
        for trim in range(0, 400):  # некоторые длины дают рассогласование на 1 кадр — подрезаем
            try: e = F.embed_clips(seg[: len(seg) - trim][None].astype(np.int16), batch_size=1)[0]; break
            except ValueError: continue
        wins += [e[i: i + 16] for i in range(len(e) - 15)]
    wins = np.array(wins)
    return clf.predict_proba((flat(wins) - mean) / std)[:, 1], wins


def concat(clips):
    out = []
    for c in clips: out += [c.astype(np.float32) * rng.uniform(0.5, 2.0), noise_clip(int(SR * rng.uniform(0.2, 0.8)))]
    return tofix(np.concatenate(out))


mine_stream = [bg_tr, neg_tr, concat(TN + TN2)]
clf = fit(Z, yy)
for rnd in range(2):
    new = []
    for s in mine_stream:
        pr, wins = stream_probs(clf, s)
        new += list(wins[np.where(pr > 0.15)[0]])
    print(f"раунд {rnd+1}: трудных негативов {len(new)}", flush=True)
    if not new: break
    new = np.array(new)
    Z = np.vstack((Z, np.repeat((flat(new) - mean) / std, 3, axis=0))); yy = np.concatenate((yy, np.zeros(len(new) * 3, int)))
    clf = fit(Z, yy)

print("\n== отложенные реальные слова (потоком)")
hits = [stream_probs(clf, skat[int((p - 2.0) * SR): int((p + 1.5) * SR)])[0].max() for p in test_words]
print("  max prob per word:", [round(float(v), 2) for v in hits])
sets = {"bg (ваша жизнь, 20%)": bg_te, "речь": neg_te, "TTS": concat(TN2_te)}
prs = {k: stream_probs(clf, s)[0] for k, s in sets.items()}
mins = {k: len(s) / SR / 60 for k, s in sets.items()}
print("\n== отложенный негатив:", {k: f"{v:.1f} мин" for k, v in mins.items()})
print("  thr   TPR(слова)   ложных/час: bg   речь   TTS")
for thr in (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99):
    def trig(p):
        idx = np.where(p > thr)[0]; return 0 if not len(idx) else 1 + int(np.sum(np.diff(idx) > 19))
    fa = {k: trig(prs[k]) / mins[k] * 60 for k in sets}
    print(f"  {thr:.2f}  {np.mean([v > thr for v in hits]):5.2f}              {fa['bg (ваша жизнь, 20%)']:6.1f} {fa['речь']:6.1f} {fa['TTS']:6.1f}")
np.savez("skat_model.npz", mean=mean.astype(np.float32), std=std.astype(np.float32),
         W1=clf.coefs_[0].astype(np.float32), b1=clf.intercepts_[0].astype(np.float32),
         W2=clf.coefs_[1].astype(np.float32), b2=clf.intercepts_[1].astype(np.float32))
print("saved skat_model.npz")
