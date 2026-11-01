"""Exports the libm calls whose bits the wasm and native builds must share.

Scalar entry points plus buffer loops (addresses are linear-memory offsets), so a
million samples cost one call."""
from std.math import sin, cos, fma
from std.memory import UnsafePointer


def f32p(a: Int) -> UnsafePointer[Float32, MutAnyOrigin]:
    return UnsafePointer[Float32, MutAnyOrigin](unsafe_from_address=a)


def f64p(a: Int) -> UnsafePointer[Float64, MutAnyOrigin]:
    return UnsafePointer[Float64, MutAnyOrigin](unsafe_from_address=a)


@export
def k_sinf(x: Float32) abi("C") -> Float32:
    return sin(x)


@export
def k_fmaf(a: Float32, b: Float32, c: Float32) abi("C") -> Float32:
    return fma(a, b, c)


@export
def k_sin_buf(src: Int, dst: Int, n: Int) abi("C"):
    var s = f32p(src)
    var d = f32p(dst)
    for i in range(n):
        d[i] = sin(s[i])


@export
def k_cos_buf(src: Int, dst: Int, n: Int) abi("C"):
    var s = f32p(src)
    var d = f32p(dst)
    for i in range(n):
        d[i] = cos(s[i])


@export
def k_fmaf_buf(a: Int, b: Int, c: Int, dst: Int, n: Int) abi("C"):
    var pa = f32p(a)
    var pb = f32p(b)
    var pc = f32p(c)
    var d = f32p(dst)
    for i in range(n):
        d[i] = fma(pa[i], pb[i], pc[i])


@export
def k_fma_buf(a: Int, b: Int, c: Int, dst: Int, n: Int) abi("C"):
    var pa = f64p(a)
    var pb = f64p(b)
    var pc = f64p(c)
    var d = f64p(dst)
    for i in range(n):
        d[i] = fma(pa[i], pb[i], pc[i])


def main():
    print(k_sinf(1.0))
