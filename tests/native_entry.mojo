"""One entry, built --target wasm64 and --target native from the same wasm.toml."""
from std.ffi import external_call


@export
def entry_sum(a: Int32, b: Int32) abi("C") -> Int32:
    return external_call["fixture_add", Int32](a, b)


def main():
    print(entry_sum(20, 22))
