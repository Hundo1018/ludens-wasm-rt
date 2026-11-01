"""Touches both host imports: stdout (host_write) and the clock (host_now_ns)."""
from std.time import monotonic


@export
def probe_print() abi("C"):
    print("hello from wasm")


@export
def probe_now() abi("C") -> Int64:
    return Int64(monotonic())


def main():
    probe_print()
