"""graph.json + weights.npz (tflite_export.py) -> kws.onnx: потоковые состояния становятся входами и выходами модели.
Запуск на ПК: python tflite_to_onnx.py marusia marusia/kws.onnx"""
import json, os, sys
import numpy as np
import onnx
from onnx import TensorProto as TP, helper as H, numpy_helper as NH


def build(d, out):
    G = json.load(open(os.path.join(d, "graph.json"))); W = np.load(os.path.join(d, "weights.npz")); g = G[0]
    nodes, inits, n = [], [], [0]

    def new(p="t"):
        n[0] += 1; return f"{p}{n[0]}"

    def const(arr, name=None):
        name = name or new("c"); inits.append(NH.from_array(np.asarray(arr), name)); return name

    cache = {}

    def cst(i):
        k = f"s0_t{i}"
        if k not in W.files: return None
        if i not in cache: cache[i] = const(W[k], f"w{i}")
        return cache[i]

    env = {g["inputs"][0]: "x"}; handles, cur, init_state = {}, {}, {}
    h1 = {}
    for op in G[1]["ops"]:
        if op["op"] == "VAR_HANDLE": h1[op["out"][0]] = op["opt"]["name"]
        elif op["op"] == "ASSIGN_VARIABLE": init_state[h1[op["in"][0]]] = W[f"s1_t{op['in'][1]}"]
    for nm, arr in init_state.items():
        cur[nm] = "st_" + str(len(cur)); init_state[nm] = (cur[nm], arr)

    def T(x): return env[x] if x in env else cst(x)

    def transpose(x, perm):
        o = new(); nodes.append(H.make_node("Transpose", [x], [o], perm=perm)); return o

    def relu(x, a):
        if a == 1: o = new(); nodes.append(H.make_node("Relu", [x], [o])); return o
        if a == 3: o = new(); nodes.append(H.make_node("Clip", [x, const(np.float32(0)), const(np.float32(6))], [o])); return o
        return x

    def same(inp, k, s):
        o_ = -(-inp // s); tot = max((o_ - 1) * s + k - inp, 0); return tot // 2, tot - tot // 2

    for op in g["ops"]:
        c, ins, outs, o = op["op"], op["in"], op["out"], op["opt"]
        if c == "CALL_ONCE": continue
        if c == "VAR_HANDLE": handles[outs[0]] = o["name"]; continue
        if c == "READ_VARIABLE": env[outs[0]] = cur[handles[ins[0]]]; continue
        if c == "ASSIGN_VARIABLE": cur[handles[ins[0]]] = T(ins[1]); continue
        r = new()
        if c == "RESHAPE":
            sh = W[f"s0_t{ins[1]}"] if len(ins) > 1 and f"s0_t{ins[1]}" in W.files else np.array(o["shape"])
            nodes.append(H.make_node("Reshape", [T(ins[0]), const(sh.astype(np.int64))], [r]))
        elif c == "PAD":
            p = W[f"s0_t{ins[1]}"].astype(np.int64); nodes.append(H.make_node("Pad", [T(ins[0]), const(np.r_[p[:, 0], p[:, 1]])], [r]))
        elif c == "CONCATENATION":
            nodes.append(H.make_node("Concat", [T(i) for i in ins], [r], axis=o["axis"]))
        elif c == "STRIDED_SLICE":
            b, e, s = (W[f"s0_t{i}"].astype(np.int64) for i in ins[1:4]); st, en, ax, sp = [], [], [], []
            for dm in range(len(b)):
                st.append(0 if (o["bm"] >> dm) & 1 else int(b[dm])); en.append(2 ** 31 - 1 if (o["em"] >> dm) & 1 else int(e[dm])); ax.append(dm); sp.append(int(s[dm]))
            nodes.append(H.make_node("Slice", [T(ins[0]), const(np.array(st, np.int64)), const(np.array(en, np.int64)), const(np.array(ax, np.int64)), const(np.array(sp, np.int64))], [r]))
        elif c in ("CONV_2D", "DEPTHWISE_CONV_2D"):
            w = W[f"s0_t{ins[1]}"]; bias = W[f"s0_t{ins[2]}"]; x = T(ins[0]); shp = g["shapes"][str(ins[0])]
            kh, kw = w.shape[1:3]; eh, ew = (kh - 1) * o["dh"] + 1, (kw - 1) * o["dw"] + 1
            if o["pad"] == 0: t, bt = same(shp[1], eh, o["sh"]); l, rr = same(shp[2], ew, o["sw"]); pads = [t, l, bt, rr]
            else: pads = [0, 0, 0, 0]
            if c == "CONV_2D": wt = const(np.transpose(w, (0, 3, 1, 2)).copy()); grp = 1
            else: wt = const(np.transpose(w, (3, 0, 1, 2)).copy()); grp = int(w.shape[3])
            xn = transpose(x, [0, 3, 1, 2]); y = new()
            nodes.append(H.make_node("Conv", [xn, wt, const(bias.astype(np.float32))], [y], strides=[o["sh"], o["sw"]], dilations=[o["dh"], o["dw"]], pads=pads, group=grp, kernel_shape=[kh, kw]))
            r = relu(transpose(y, [0, 2, 3, 1]), o["act"])
        elif c == "MAX_POOL_2D":
            shp = g["shapes"][str(ins[0])]
            if o["pad"] == 0: t, bt = same(shp[1], o["fh"], o["sh"]); l, rr = same(shp[2], o["fw"], o["sw"]); pads = [t, l, bt, rr]
            else: pads = [0, 0, 0, 0]
            y = new(); nodes.append(H.make_node("MaxPool", [transpose(T(ins[0]), [0, 3, 1, 2])], [y], kernel_shape=[o["fh"], o["fw"]], strides=[o["sh"], o["sw"]], pads=pads))
            r = transpose(y, [0, 2, 3, 1])
        elif c in ("MUL", "ADD"):
            y = new(); nodes.append(H.make_node("Mul" if c == "MUL" else "Add", [T(ins[0]), T(ins[1])], [y])); r = relu(y, o["act"])
        elif c == "LOGISTIC": nodes.append(H.make_node("Sigmoid", [T(ins[0])], [r]))
        elif c == "SOFTMAX": nodes.append(H.make_node("Softmax", [T(ins[0])], [r], axis=-1))
        else: raise NotImplementedError(c)
        env[outs[0]] = r
    # выходы: две пары вероятностей и новые состояния
    out_names = []
    for k, i in enumerate(g["outputs"]):
        nodes.append(H.make_node("Identity", [env[i]], [f"out{k}"])); out_names.append(f"out{k}")
    st_in, st_out = [], []
    for nm, (iname, arr) in init_state.items():
        st_in.append(H.make_tensor_value_info(iname, TP.FLOAT, list(arr.shape)))
        nodes.append(H.make_node("Identity", [cur[nm]], ["n" + iname])); st_out.append(H.make_tensor_value_info("n" + iname, TP.FLOAT, list(arr.shape)))
    graph = H.make_graph(nodes, "kws", [H.make_tensor_value_info("x", TP.FLOAT, [1, 8, 40])] + st_in,
                         [H.make_tensor_value_info(nme, TP.FLOAT, [1, 8, 2]) for nme in out_names] + st_out, inits)
    model = H.make_model(graph, opset_imports=[H.make_opsetid("", 13)]); model.ir_version = 8
    onnx.checker.check_model(model); onnx.save(model, out)
    np.savez(out.replace(".onnx", "_state.npz"), **{iname: arr for nm, (iname, arr) in init_state.items()})
    print("сохранено", out, "узлов", len(nodes), "состояний", len(st_in))


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
