"""Pins what `c_fn_ptr` yields on wasm64: a function-table index, not a code address.

Generalised from wgpu-mojo tests/wasm/fnptr_probe.mojo. The second callback takes a 16-byte
(pointer, length) struct by value -- the shape of WGPUStringView -- which lowers to two i64
parameters, so a JS host calls it through the table as (status, ptr, len, ud1, ud2)."""
from std.memory import OpaquePointer, Pointer
from std.sys import size_of


def c_fn_ptr[F: TrivialRegisterPassable](f: F) -> OpaquePointer[MutUntrackedOrigin]:
    """The address of an `abi("C")` function; on wasm64 its function-table index."""
    comptime assert size_of[F]() == size_of[OpaquePointer[MutUntrackedOrigin]](), (
        "c_fn_ptr takes an abi(\"C\") function value"
    )
    var slot = f
    return Pointer(to=slot).unsafe_bitcast[OpaquePointer[MutUntrackedOrigin]]()[]


@fieldwise_init
struct StrView(TrivialRegisterPassable):
    var data: Pointer[NoneType, MutUntrackedOrigin]
    var length: UInt


def add1(x: Int32) abi("C") -> Int32:
    return x + 1


def view_callback(
    status: UInt32,
    message: StrView,
    userdata1: OpaquePointer[MutUntrackedOrigin],
    userdata2: OpaquePointer[MutUntrackedOrigin],
) abi("C"):
    userdata1.unsafe_bitcast[UInt32]()[] = status


@export
def fnptr_add1() abi("C") -> UInt64:
    return UInt64(Int(c_fn_ptr(add1)))


@export
def fnptr_view_callback() abi("C") -> UInt64:
    return UInt64(Int(c_fn_ptr(view_callback)))


def main():
    pass
