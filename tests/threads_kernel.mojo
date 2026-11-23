"""Fixture for test_threads.py: a --threads module run from several instances on one shared memory.

Each item allocates and frees a List (so concurrent instances meet in the runtime's allocator) and writes one
deterministic Float64; the result must not depend on how the items are split across instances."""
from std.math import sin


@export
def threads_fill(base: UInt64, lo: Int64, hi: Int64) abi("C") -> Int64:
    """out[i] for i in [lo, hi), out at address base."""
    var out = Pointer[Float64, MutUntrackedOrigin](unsafe_from_address=Int(base))
    for i in range(Int(lo), Int(hi)):
        var n = 16 + i % 48
        var xs = List[Float64](capacity=n)
        for k in range(n):
            xs.append(sin(Float64(i * 64 + k) * 0.001))
        var s = Float64(0)
        for r in range(50):
            for k in range(len(xs)):
                s = s * 0.999 + xs[k] * Float64(r)
        (out + i)[] = s
    return 0
