"""Запуск потоковой TFLite-модели детектора слова из заводской прошивки на чистом numpy (граф и веса: tflite_export.py).
Вход одного вызова: признаки [1, 8, 40]; выход: две пары вероятностей [1, 8, 2] (pos_neg_probs, int_unint_probs). Состояния (VAR_HANDLE) хранятся между вызовами."""
import json, os
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv


def act(x, a):
    return np.maximum(x, 0) if a == 1 else np.clip(x, 0, 6) if a == 3 else x


def same_pad(inp, k, s):
    out = -(-inp // s); tot = max((out - 1) * s + k - inp, 0); return tot // 2, tot - tot // 2


class KWS:
    def __init__(self, d):
        self.g = json.load(open(os.path.join(d, "graph.json"))); self.W = np.load(os.path.join(d, "weights.npz"))
        self.vars = {}; self.debug = False; self.reset()

    def const(self, si, i):
        k = f"s{si}_t{i}"; return self.W[k] if k in self.W.files else None

    def reset(self):
        h = {}
        for op in self.g[1]["ops"]:
            if op["op"] == "VAR_HANDLE": h[op["out"][0]] = op["opt"]["name"]
            elif op["op"] == "ASSIGN_VARIABLE": self.vars[h[op["in"][0]]] = self.const(1, op["in"][1]).copy()

    def __call__(self, x):
        g = self.g[0]; env = {g["inputs"][0]: np.asarray(x, np.float32)}; h = {}

        def get(i): return env[i] if i in env else self.const(0, i)
        import time as _t
        for op in g["ops"]:
            c, ins, outs, o = op["op"], op["in"], op["out"], op["opt"]
            _t0 = _t.perf_counter()
            if c == "CALL_ONCE": continue
            if c == "VAR_HANDLE": h[outs[0]] = o["name"]; continue
            if c == "READ_VARIABLE": env[outs[0]] = self.vars[h[ins[0]]].copy(); continue
            if c == "ASSIGN_VARIABLE": self.vars[h[ins[0]]] = get(ins[1]).copy(); continue
            a = get(ins[0])
            if c == "RESHAPE":
                sh = self.const(0, ins[1]) if len(ins) > 1 and ins[1] >= 0 and self.const(0, ins[1]) is not None else o["shape"]
                r = a.reshape([int(v) for v in sh])
            elif c == "PAD":
                r = np.pad(a, [(int(p[0]), int(p[1])) for p in get(ins[1])])
            elif c == "CONCATENATION":
                r = np.concatenate([get(i) for i in ins], axis=o["axis"])
            elif c == "STRIDED_SLICE":
                b, e, s = (get(i).tolist() for i in ins[1:4]); idx = []
                for d in range(a.ndim):
                    lo = None if (o["bm"] >> d) & 1 else b[d]; hi = None if (o["em"] >> d) & 1 else e[d]; idx.append(slice(lo, hi, s[d]))
                r = a[tuple(idx)]
            elif c in ("CONV_2D", "DEPTHWISE_CONV_2D"):
                w, bias = get(ins[1]), get(ins[2]); kh, kw = w.shape[1:3]; sh, sw, dh, dw = o["sh"], o["sw"], o["dh"], o["dw"]
                eh, ew = (kh - 1) * dh + 1, (kw - 1) * dw + 1
                if o["pad"] == 0:
                    t, bm = same_pad(a.shape[1], eh, sh); l, rr = same_pad(a.shape[2], ew, sw); a = np.pad(a, ((0, 0), (t, bm), (l, rr), (0, 0)))
                oh, ow = (a.shape[1] - eh) // sh + 1, (a.shape[2] - ew) // sw + 1; n = a.shape[0]
                if c == "CONV_2D":
                    r = np.zeros((n, oh, ow, w.shape[0]), np.float32)
                    for y in range(kh):
                        for x_ in range(kw):
                            sl = a[:, y * dh: y * dh + (oh - 1) * sh + 1: sh, x_ * dw: x_ * dw + (ow - 1) * sw + 1: sw, :]
                            r += (sl.reshape(-1, sl.shape[-1]) @ w[:, y, x_, :].T).reshape(n, oh, ow, -1)
                else:
                    wd = w[0]; r = np.zeros((n, oh, ow, a.shape[3]), np.float32)
                    for y in range(kh):
                        for x_ in range(kw):
                            r += a[:, y * dh: y * dh + (oh - 1) * sh + 1: sh, x_ * dw: x_ * dw + (ow - 1) * sw + 1: sw, :] * wd[y, x_]
                r = act(r + bias, o["act"])
            elif c == "MAX_POOL_2D":
                fh, fw = o["fh"], o["fw"]
                if o["pad"] == 0:
                    t, bm = same_pad(a.shape[1], fh, o["sh"]); l, rr = same_pad(a.shape[2], fw, o["sw"]); a = np.pad(a, ((0, 0), (t, bm), (l, rr), (0, 0)), constant_values=-np.inf)
                r = swv(a, (fh, fw), axis=(1, 2))[:, ::o["sh"], ::o["sw"]].max(axis=(-1, -2))
            elif c == "MUL": r = act(a * get(ins[1]), o["act"])
            elif c == "ADD": r = act(a + get(ins[1]), o["act"])
            elif c == "LOGISTIC": r = 1 / (1 + np.exp(-a))
            elif c == "SOFTMAX": e_ = np.exp((a - a.max(-1, keepdims=True)) * o.get("beta", 1.0)); r = e_ / e_.sum(-1, keepdims=True)
            else: raise NotImplementedError(c)
            if getattr(self, "prof", None) is not None: self.prof[c] = self.prof.get(c, 0.0) + _t.perf_counter() - _t0
            if getattr(self, "trace", None) is not None: self.trace.append((g["ops"].index(op), c, float(np.nanmax(np.abs(r))) if r.size else 0.0, bool(np.isnan(r).any())))
            if self.debug and [int(v) for v in r.shape] != g["shapes"][str(outs[0])]:
                raise ValueError(f"оператор #{g['ops'].index(op)} {c}: получилось {list(r.shape)}, в графе {g['shapes'][str(outs[0])]}; опции {o}; вход {list(a.shape)}")
            env[outs[0]] = r.astype(np.float32) if r.dtype == np.float64 else r
        return [env[i] for i in g["outputs"]]


if __name__ == "__main__":
    import sys, time
    k = KWS(sys.argv[1]); k.debug = True; x = (np.random.randn(1, 8, 40) * 0.3).astype(np.float32)
    t0 = time.time(); outs = k(x); dt = time.time() - t0
    print("выходы:", [o.shape for o in outs], "за %.1f мс" % (dt * 1000)); print(outs[0][0, :3], outs[1][0, :3])
    # проверка форм по графу
    g = k.g[0]
    env_ok = True
    for t_, v in ((i, s) for i, s in g["shapes"].items()): pass
    t0 = time.time()
    for _ in range(20): k(x)
    print("среднее на вызов: %.1f мс" % ((time.time() - t0) / 20 * 1000))
