"""Локальный детектор слова «Скат»: потоковые признаки openWakeWord (melspectrogram.onnx -> embedding_model.onnx,
80 мс на шаг) + маленький MLP из skat_model.npz. Работает на колонке (onnxruntime + numpy) и на сервере для оценки.
feed(chunk) принимает ровно 1280 отсчётов int16 16 кГц и возвращает вероятность слова (или None, пока копятся признаки)."""
import numpy as np
import onnxruntime as ort

STEP = 1280  # 80 мс


class Wake:
    def __init__(self, d="/opt/skat/ww"):
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        prov = ["CPUExecutionProvider"]
        self.mel = ort.InferenceSession(f"{d}/melspectrogram.onnx", so, providers=prov)
        self.emb = ort.InferenceSession(f"{d}/embedding_model.onnx", so, providers=prov)
        self.mi, self.ei = self.mel.get_inputs()[0].name, self.emb.get_inputs()[0].name
        import os
        m = np.load(f"{d}/skat_model5.npz" if os.path.exists(f"{d}/skat_model5.npz") else f"{d}/skat_model.npz")
        self.mean, self.std, self.W1, self.b1, self.W2, self.b2 = (m[k] for k in ("mean", "std", "W1", "b1", "W2", "b2"))
        self.p = np.zeros(5)
        self.reset()

    def reset(self):
        self.raw = np.zeros(0, np.float32)
        self.mels = np.zeros((0, 32), np.float32)
        self.embs = np.zeros((0, 96), np.float32)

    def feed(self, chunk):
        self.raw = np.concatenate((self.raw, chunk.astype(np.float32)))[-(STEP + 480):]
        mel = np.squeeze(self.mel.run(None, {self.mi: self.raw[None]})[0]) / 10 + 2
        self.mels = np.vstack((self.mels, mel.reshape(-1, 32)))[-120:]
        if len(self.mels) < 76: return None
        e = self.emb.run(None, {self.ei: self.mels[-76:][None, :, :, None].astype(np.float32)})[0].reshape(1, 96)
        self.embs = np.vstack((self.embs, e))[-16:]
        if len(self.embs) < 16: return None
        x = (self.embs.reshape(-1) - self.mean) / self.std
        h = np.maximum(x @ self.W1 + self.b1, 0)
        z = h @ self.W2 + self.b2
        if len(z) == 1: self.p = np.array([0.0, 1 / (1 + np.exp(-z[0]))]); return float(self.p[1])
        e = np.exp(z - z.max()); self.p = e / e.sum()  # классы: 0 фон, 1 скат, 2 дальше, 3 назад, 4 пауза
        return float(self.p[1])
