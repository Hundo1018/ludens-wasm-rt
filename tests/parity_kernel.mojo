"""Fixture for ludens_wasm_rt.parity: writes [3 k, 2^64 - 1, k] and returns 0; a negative k returns 1."""


@export
def parity_probe(k: Int32, out_ptr: UInt64) abi("C") -> Int32:
    if k < 0:
        return 1
    var out = Pointer[UInt64, MutUntrackedOrigin](unsafe_from_address=Int(out_ptr))
    out[0] = UInt64(3 * Int(k))
    out[1] = UInt64(0xFFFFFFFFFFFFFFFF)
    out[2] = UInt64(Int(k))
    return 0


def main():
    pass
