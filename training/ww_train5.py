"""Версия 5: мульти-класс (0 фон, 1 скат, 2 дальше, 3 назад, 4 пауза) + открытая речь в негативах. Основа — версия 4: + музыка. Обучение детектора слова «Скат» поверх эмбеддингов openWakeWord (16 кадров x 96 на 2 с звука).
Добавлено: записи после эхоподавления (music_c*.raw: остаток музыки + «Скат» вашим голосом) — фон для всех
классов и реальные положительные примеры. Было (версия 3):
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
import glob
MUS = [np.fromfile(f, np.int16) for f in sorted(glob.glob("music_c*.raw"))]  # 60-секундные куски после эхоподавления


def music_peaks(x):
    e = np.sqrt(np.convolve(x.astype(np.float32) ** 2, np.ones(160) / 160, "same"))[::160]
    sm = np.convolve(e, np.ones(25) / 25, "same"); base = np.median(sm); pk = []
    for i in range(30, len(sm) - 30):
        if sm[i] > 2.2 * base and sm[i] == sm[max(0, i - 75): i + 76].max() and (not pk or i - pk[-1] > 150): pk.append(i)
    return [p / 100 for p in pk]


MUS_PK = [music_peaks(x) for x in MUS]
MUS_TR, MUS_TE = list(range(len(MUS) - 1)), [len(MUS) - 1]      # последний кусок — на проверку


def free_bank(idxs):
    """Куски музыкального остатка без слов (дальше 1,3 с от пиков)."""
    out = []
    for k in idxs:
        x, pk = MUS[k], MUS_PK[k]; a = 0.0
        for p in pk + [len(x) / SR]:
            if p - 1.3 - a > 1.0: out.append(x[int(a * SR): int((p - 1.3) * SR)])
            a = p + 1.3
    return np.concatenate(out) if out else np.zeros(SR * 5, np.int16)


mus_bank_tr, mus_bank_te = free_bank(MUS_TR), free_bank(MUS_TE)
print(f"музыка: куски {[len(x)//SR for x in MUS]} с, слов {[len(p) for p in MUS_PK]}, банк остатка train {len(mus_bank_tr)/SR:.0f} с, test {len(mus_bank_te)/SR:.0f} с", flush=True)
tts = np.load("tts.npz", allow_pickle=True)
TP, TN = list(tts["pos"]), list(tts["neg"])
TN2 = list(np.load("neg2.npz", allow_pickle=True)["neg"])
rng.shuffle(TN2); hold = int(len(TN2) * 0.15); TN2_te, TN2 = TN2[:hold], TN2[hold:]
CMD = np.load("cmd_tts.npz", allow_pickle=True); CMDL = {2: list(CMD["dalshe"]), 3: list(CMD["nazad"]), 4: list(CMD["pauza"])}
CMDH = {}
for k in CMDL: rng.shuffle(CMDL[k]); h = max(3, int(len(CMDL[k]) * 0.15)); CMDH[k], CMDL[k] = CMDL[k][:h], CMDL[k][h:]
OPEN = np.fromfile("open_train.raw", np.int16); OPEN_TE = np.fromfile("open_test.raw", np.int16); CONF = np.fromfile("open_conf.raw", np.int16)
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


def music_clip(bank, n=CLIP):
    i = rng.integers(0, len(bank) - n); return bank[i: i + n].astype(np.float32)


def with_music(x, bank, p=0.5):
    """С вероятностью p подмешиваем остаток музыки (уровень 0,5-1,6 от записанного)."""
    return x + music_clip(bank) * rng.uniform(0.5, 1.6) if rng.random() < p else x


def put_word(word, end_off, snr, bank=None):
    bg_ = noise_clip() * rng.uniform(0.7, 1.6)
    bg_ = with_music(bg_, mus_bank_tr if bank is None else bank)
    wd = word.astype(np.float32); wd *= (rms(bg_) * 10 ** (snr / 20) / rms(wd))
    end = CLIP - int(end_off * SR); start = end - len(wd)
    if start < 0: wd, start = wd[-start:], 0
    bg_[start: start + len(wd)] += wd[: CLIP - start]
    return bg_


def real_clip(p, end_off):
    end = int((p + 0.28 + end_off) * SR); x = skat[end - CLIP: end].astype(np.float32)
    return with_music(x * rng.uniform(0.5, 2.0) + noise_clip() * rng.uniform(0, 0.6), mus_bank_tr, 0.5)


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
for k in MUS_TR:      # реальные «Скат» поверх музыки (после эхоподавления)
    for p in MUS_PK[k]:
        for _ in range(40):
            end = int((p + 0.28 + rng.uniform(0.05, 0.55)) * SR)
            if end - CLIP < 0 or end > len(MUS[k]): continue
            add(MUS[k][end - CLIP: end].astype(np.float32) * rng.uniform(0.6, 1.8), 1)
for _ in range(2500): add(music_clip(mus_bank_tr) * rng.uniform(0.5, 1.8), 0)       # просто остаток музыки
for _ in range(2500):                                                                # речь + остаток музыки
    i = rng.integers(0, len(neg_tr) - CLIP); add(neg_tr[i: i + CLIP].astype(np.float32) * rng.uniform(0.5, 2.0) + music_clip(mus_bank_tr) * rng.uniform(0.4, 1.6), 0)
for _ in range(2500):                                                                # фон + остаток музыки
    i = rng.integers(0, len(bg_tr) - CLIP); add(bg_tr[i: i + CLIP].astype(np.float32) * rng.uniform(0.6, 2.5) + music_clip(mus_bank_tr) * rng.uniform(0.4, 1.6), 0)
for k, cl in CMDL.items():      # командные слова: TTS + шум/музыка
    for wd in cl:
        for _ in range(8): add(put_word(wd, rng.uniform(0.1, 0.55), rng.uniform(2, 16)), k)
        add(put_word(wd[: int(len(wd) * rng.uniform(0.3, 0.6))], rng.uniform(0.0, 0.15), rng.uniform(2, 16)), 0)  # обрезок - не команда
for src, n in ((OPEN, 6000), (CONF, 2500)):
    for _ in range(n):
        i = rng.integers(0, len(src) - CLIP); add(src[i: i + CLIP].astype(np.float32) * rng.uniform(0.5, 2.0), 0)
for _ in range(2500):         # открытая речь + остаток музыки
    i = rng.integers(0, len(OPEN) - CLIP); add(OPEN[i: i + CLIP].astype(np.float32) * rng.uniform(0.5, 2.0) + music_clip(mus_bank_tr) * rng.uniform(0.4, 1.6), 0)
flush()
E = np.concatenate(Es); y = np.array(ys)
print("embeddings", E.shape, "классы", np.bincount(y).tolist(), f"{time.time()-t0:.0f}s", flush=True)
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
    return clf.predict_proba((flat(wins) - mean) / std), wins


def concat(clips):
    out = []
    for c in clips: out += [c.astype(np.float32) * rng.uniform(0.5, 2.0), noise_clip(int(SR * rng.uniform(0.2, 0.8)))]
    return tofix(np.concatenate(out))


mine_stream = [bg_tr, neg_tr, concat(TN + TN2), OPEN[: 40 * 60 * SR], CONF]
clf = fit(Z, yy)
for rnd in range(2):
    new = []
    for s in mine_stream:
        pr, wins = stream_probs(clf, s)
        new += list(wins[np.where(1 - pr[:, 0] > 0.15)[0]])
    print(f"раунд {rnd+1}: трудных негативов {len(new)}", flush=True)
    if not new: break
    new = np.array(new)
    Z = np.vstack((Z, np.repeat((flat(new) - mean) / std, 3, axis=0))); yy = np.concatenate((yy, np.zeros(len(new) * 3, int)))
    clf = fit(Z, yy)

np.savez("skat_model5.npz", mean=mean.astype(np.float32), std=std.astype(np.float32),
         W1=clf.coefs_[0].astype(np.float32), b1=clf.intercepts_[0].astype(np.float32),
         W2=clf.coefs_[1].astype(np.float32), b2=clf.intercepts_[1].astype(np.float32))
print("saved skat_model5.npz")
NAMES = {1: "скат", 2: "дальше", 3: "назад", 4: "пауза"}
def mx(x, c): return stream_probs(clf, x)[0][:, c].max()
print("\n== слова поверх музыки (скат)")
x3, pk3 = MUS[MUS_TE[0]], MUS_PK[MUS_TE[0]]
mh = [mx(x3[max(0, int((p - 1.5) * SR)): int((p + 1.5) * SR)], 1) for p in pk3 if 1.5 < p < len(x3) / SR - 1.5]
print("  max prob:", [round(float(v), 2) for v in mh])
hits = {1: [mx(skat[int((p - 2.0) * SR): int((p + 1.5) * SR)], 1) for p in test_words]}
pad = lambda c: np.concatenate((noise_clip(SR), c.astype(np.float32), noise_clip(SR)))  # поток короче 3 с не считается
for k in CMDH: hits[k] = [mx(tofix(pad(put_word(w, 0.3, 8))), k) for w in CMDH[k]]
print("== TPR (thr 0.9) на отложенных:", {NAMES[k]: round(float(np.mean([v > 0.9 for v in h])), 2) for k, h in hits.items()})
sets = {"open_test": OPEN_TE, "bg": bg_te, "речь": neg_te, "TTS": concat(TN2_te), "музыка": mus_bank_te,
        "open_test+музыка": tofix(OPEN_TE.astype(np.float32) + np.tile(mus_bank_te, 1 + len(OPEN_TE) // len(mus_bank_te))[: len(OPEN_TE)].astype(np.float32))}
prs = {k: stream_probs(clf, s_)[0] for k, s_ in sets.items()}
mins = {k: len(s_) / SR / 60 for k, s_ in sets.items()}
for thr in (0.8, 0.9, 0.95, 0.98, 0.99):
    def trig(p):
        idx = np.where(p > thr)[0]; return 0 if not len(idx) else 1 + int(np.sum(np.diff(idx) > 19))
    print(f"  thr {thr}: ложных/час [скат дальше назад пауза]:")
    for k in sets: print("     ", k.ljust(18), [round(trig(prs[k][:, c]) / mins[k] * 60, 1) for c in (1, 2, 3, 4)])
