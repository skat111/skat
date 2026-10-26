"""Детектор слова «Маруся» из заводской прошивки колонки (потоковая модель model.bin, переведённая в ONNX: kws.onnx + kws_state.npz).
Признаки найдены подбором: логарифмическая мел-шкала в децибелах (40 фильтров, 20-8000 Гц, окно 25 мс, шаг 10 мс, спектр мощности, сигнал в масштабе int16).
feed(chunk) принимает ровно 1280 отсчётов int16 16 кГц (80 мс = 8 кадров) и возвращает уверенность 0..1 (минимум из двух выходов модели)."""
import os
import numpy as np
import onnxruntime as ort

WIN, HOP, NFFT, NMEL = 400, 160, 512, 40


def mel_fb(sr=16000, fmin=20.0, fmax=8000.0):
    hz2mel = lambda f: 2595 * np.log10(1 + f / 700)
    mel2hz = lambda m: 700 * (10 ** (m / 2595) - 1)
    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), NMEL + 2)); fr = np.fft.rfftfreq(NFFT, 1 / sr)
    fb = np.zeros((NMEL, len(fr)), np.float32)
    for i in range(NMEL):
        lo, c, hi = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.maximum(0, np.minimum((fr - lo) / (c - lo), (hi - fr) / (hi - c)))
    return fb


class Marusia:
    def __init__(self, d="/opt/skat/ww/marusia"):
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        self.s = ort.InferenceSession(os.path.join(d, "kws.onnx"), so, providers=["CPUExecutionProvider"])
        self.outs = [o.name for o in self.s.get_outputs()]
        st = np.load(os.path.join(d, "kws_state.npz")); self.st0 = {k: st[k].copy() for k in st.files}
        self.mel, self.win = mel_fb(), np.hanning(WIN).astype(np.float32)
        self.reset()

    def reset(self):
        self.state = {k: v.copy() for k, v in self.st0.items()}; self.buf = np.zeros(WIN - HOP, np.float32)

    def feed(self, chunk):
        x = np.concatenate((self.buf, chunk.astype(np.float32))); self.buf = x[-(WIN - HOP):]
        fr = np.stack([x[i * HOP: i * HOP + WIN] for i in range(8)]) * self.win
        S = np.abs(np.fft.rfft(fr, NFFT, axis=1)) ** 2
        f = (10 * np.log10(S @ self.mel.T + 1e-3)).astype(np.float32)[None]
        res = self.s.run(self.outs, {"x": f, **self.state})
        for o, v in zip(self.outs[2:], res[2:]): self.state[o[1:]] = v    # nst_N -> st_N
        return float(np.min((res[0][0, :, 1], res[1][0, :, 1]), axis=0).max())  # лучший из 8 кадров, оба выхода должны согласиться
