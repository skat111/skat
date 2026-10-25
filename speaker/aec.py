"""Эхоподавление на Speex (libspeexdsp, тот же, что в заводской прошивке) через ctypes.
Кадры по 320 отсчётов int16 16 кГц (20 мс); опорный кадр должен быть уже выровнен по времени с микрофонным
(задержка опоры относительно эха внутри длины фильтра)."""
import ctypes, numpy as np

FRAME = 320
SPEEX_ECHO_SET_SAMPLING_RATE = 24
_lib = None


def lib():
    global _lib
    if _lib is None:
        _lib = ctypes.CDLL("libspeexdsp.so.1")
        _lib.speex_echo_state_init.restype = ctypes.c_void_p
        _lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        _lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        _lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        _lib.speex_echo_state_reset.argtypes = [ctypes.c_void_p]
        _lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
    return _lib


class Aec:
    def __init__(self, filter_len=4096):
        self.L, self.fl = lib(), filter_len
        self.st = self.L.speex_echo_state_init(FRAME, filter_len)
        rate = ctypes.c_int(16000)
        self.L.speex_echo_ctl(self.st, SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(rate))
        self.out = np.zeros(FRAME, np.int16)

    def process(self, mic, ref):
        """mic, ref: np.int16[320] -> np.int16[320] (копия очищенного кадра)."""
        mic, ref = np.ascontiguousarray(mic, np.int16), np.ascontiguousarray(ref, np.int16)
        self.L.speex_echo_cancellation(self.st, mic.ctypes.data, ref.ctypes.data, self.out.ctypes.data)
        return self.out.copy()

    def reset(self):
        self.L.speex_echo_state_reset(self.st)

    def __del__(self):
        try: self.L.speex_echo_state_destroy(self.st)
        except Exception: pass


class Live:
    """Эхоподавление на лету. Опора: поток музыки, который мы сами играем (push_ref из потока музыки, 16 кГц).
    Задержку между опорой и эхом в микрофоне колонка оценивает сама (GCC-PHAT по последним секундам) и поправляет
    по ходу дела (потеря кадров микрофона, паузы). Пока задержка неизвестна, кадры идут как есть."""
    MARGIN = 800        # опору опережаем на 50 мс: эхо приходит позже, и фильтр это моделирует
    WIN, REF = 4 * 16000, 12 * 16000

    def __init__(self, filter_len=4096):
        import threading
        self.threading = threading
        self.fl, self.lock = filter_len, threading.Lock()
        self.aec = Aec(filter_len)
        self.ref, self.ref_n = np.zeros(0, np.int16), 0
        self.reset()

    def reset(self):
        self.mic, self.mic_n, self.D, self.cand, self.busy_est = np.zeros(0, np.int16), 0, None, None, False
        self.since = 0
        self.aec.reset()

    def push_ref(self, blk):
        with self.lock:
            self.ref = np.concatenate((self.ref, blk))[-self.REF:]; self.ref_n += len(blk)

    def _estimate(self, m, ms, r, rs):
        """GCC-PHAT на 8 кГц: mic_abs - ref_abs = D, где m[j + k] ~ r[j]."""
        try:
            a = m.astype(np.float32)[: len(m) // 2 * 2].reshape(-1, 2).mean(1)
            b = r.astype(np.float32)[: len(r) // 2 * 2].reshape(-1, 2).mean(1)
            n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
            R = np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)); R /= np.abs(R) + 1e-6
            c = np.fft.irfft(R, n); ml = len(b)
            c = np.concatenate((c[-ml:], c[:len(a)]))      # лаги -len(b) .. len(a)-1
            i = int(np.argmax(c)); q = float(c[i] / (np.median(np.abs(c)) + 1e-9))
            k = (i - ml) * 2
            if q >= 15:
                D = ms + k - rs
                with self.lock:
                    if self.D is None or abs(D - self.D) < 160:
                        if self.D is None: print(f"AEC: задержка опоры {D} отсчётов (качество {q:.0f})", flush=True)
                        self.D = D if self.D is None else self.D; self.cand = None
                    elif self.cand is not None and abs(D - self.cand) < 160:  # второе подтверждение подряд — меняем
                        print(f"AEC: задержка сменилась {self.D} -> {D}", flush=True)
                        self.D, self.cand = D, None; self.aec.reset()
                    else: self.cand = D
        finally:
            self.busy_est = False

    def process(self, frame):
        """frame: np.int16[320] микрофон -> очищенный кадр (или он же, если задержка пока неизвестна)."""
        self.mic = np.concatenate((self.mic, frame))[-self.WIN:]; self.mic_n += len(frame); self.since += 1
        if self.since >= 150 and not self.busy_est and len(self.mic) >= self.WIN:  # раз в 3 с, в фоне
            with self.lock: r, rs = self.ref.copy(), self.ref_n - len(self.ref)
            if len(r) >= 6 * 16000:
                self.since, self.busy_est = 0, True
                self.threading.Thread(target=self._estimate, args=(self.mic.copy(), self.mic_n - len(self.mic), r, rs), daemon=True).start()
        if self.D is None: return frame
        end = self.mic_n - self.D + self.MARGIN
        with self.lock:
            lo = self.ref_n - len(self.ref)
            if end - 320 < lo or end > self.ref_n: return frame
            ref = self.ref[end - 320 - lo: end - lo].copy()
        return self.aec.process(frame, ref)
