"""Разбор .tflite (модель «Маруся» из заводской прошивки) в простой граф: graph.json + weights.npz — чтобы запускать на чистом numpy без TFLite.
Запуск (на ПК, нужен пакет tflite): python tflite_export.py model.bin outdir"""
import json, os, sys
import numpy as np
import tflite as T

NAMES = {getattr(T.BuiltinOperator, n): n for n in dir(T.BuiltinOperator) if n.isupper()}
DT = {0: np.float32, 1: np.float16, 2: np.int32, 3: np.uint8, 4: np.int64, 6: np.bool_, 9: np.int8}


def const(m, g, i):
    t = g.Tensors(i); b = m.Buffers(t.Buffer()); n = b.DataLength()
    if n == 0: return None
    arr = np.frombuffer(b.DataAsNumpy().tobytes(), DT[t.Type()])
    arr = arr.reshape([t.Shape(k) for k in range(t.ShapeLength())]).copy()
    q = t.Quantization()
    if t.Type() == 9 and q is not None and q.ScaleLength():  # веса int8 с масштабом: в самой модели активации float, веса «сжаты»
        sc = np.array([q.Scale(i) for i in range(q.ScaleLength())], np.float32); zp = np.array([q.ZeroPoint(i) for i in range(q.ZeroPointLength())], np.float32) if q.ZeroPointLength() else np.zeros_like(sc)
        if len(sc) == 1: return (arr.astype(np.float32) - zp[0]) * sc[0]
        sh = [1] * arr.ndim; sh[q.QuantizedDimension()] = -1
        return (arr.astype(np.float32) - zp.reshape(sh)) * sc.reshape(sh)
    return arr


def export(path, out):
    m = T.Model.GetRootAsModel(open(path, "rb").read(), 0)
    os.makedirs(out, exist_ok=True)
    weights, graphs = {}, []
    for si in range(m.SubgraphsLength()):
        g = m.Subgraphs(si); ops = []; shapes = {}
        for i in range(g.TensorsLength()):
            t = g.Tensors(i); shapes[i] = [t.Shape(k) for k in range(t.ShapeLength())]
            c = const(m, g, i)
            if c is not None: weights[f"s{si}_t{i}"] = c
        for k in range(g.OperatorsLength()):
            op = g.Operators(k); code = NAMES[m.OperatorCodes(op.OpcodeIndex()).BuiltinCode()]
            rec = {"op": code, "in": [op.Inputs(j) for j in range(op.InputsLength())], "out": [op.Outputs(j) for j in range(op.OutputsLength())], "opt": {}}
            o = op.BuiltinOptions()
            if o is not None:
                tab = op.BuiltinOptions().__class__
                if code == "CONV_2D":
                    x = T.Conv2DOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"pad": x.Padding(), "sw": x.StrideW(), "sh": x.StrideH(), "act": x.FusedActivationFunction(), "dw": x.DilationWFactor(), "dh": x.DilationHFactor()}
                elif code == "DEPTHWISE_CONV_2D":
                    x = T.DepthwiseConv2DOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"pad": x.Padding(), "sw": x.StrideW(), "sh": x.StrideH(), "act": x.FusedActivationFunction(), "dw": x.DilationWFactor(), "dh": x.DilationHFactor(), "mult": x.DepthMultiplier()}
                elif code in ("MUL", "ADD"):
                    x = (T.MulOptions if code == "MUL" else T.AddOptions)(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"act": x.FusedActivationFunction()}
                elif code == "CONCATENATION":
                    x = T.ConcatenationOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"axis": x.Axis(), "act": x.FusedActivationFunction()}
                elif code == "STRIDED_SLICE":
                    x = T.StridedSliceOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"bm": x.BeginMask(), "em": x.EndMask(), "ell": x.EllipsisMask(), "nax": x.NewAxisMask(), "shr": x.ShrinkAxisMask()}
                elif code == "MAX_POOL_2D":
                    x = T.Pool2DOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"pad": x.Padding(), "sw": x.StrideW(), "sh": x.StrideH(), "fw": x.FilterWidth(), "fh": x.FilterHeight(), "act": x.FusedActivationFunction()}
                elif code == "SOFTMAX":
                    x = T.SoftmaxOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos); rec["opt"] = {"beta": x.Beta()}
                elif code == "RESHAPE":
                    x = T.ReshapeOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"shape": [x.NewShape(j) for j in range(x.NewShapeLength())]}
                elif code == "VAR_HANDLE":
                    x = T.VarHandleOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos)
                    rec["opt"] = {"name": (x.SharedName() or b"").decode(), "container": (x.Container() or b"").decode()}
                elif code == "CALL_ONCE":
                    x = T.CallOnceOptions(); x.Init(op.BuiltinOptions().Bytes, op.BuiltinOptions().Pos); rec["opt"] = {"sub": x.InitSubgraphIndex()}
            ops.append(rec)
        graphs.append({"ops": ops, "shapes": {str(k): v for k, v in shapes.items()},
                       "inputs": [g.Inputs(j) for j in range(g.InputsLength())], "outputs": [g.Outputs(j) for j in range(g.OutputsLength())]})
    json.dump(graphs, open(os.path.join(out, "graph.json"), "w"))
    np.savez(os.path.join(out, "weights.npz"), **weights)
    print("графов", len(graphs), "констант", len(weights), "операторов", [len(g["ops"]) for g in graphs])


if __name__ == "__main__":
    export(sys.argv[1], sys.argv[2])
