"""Native-vs-wasm64 parity: run a wasm export that writes N u64 results, compare them with the native build's.

A parity export takes its scalar arguments, then a pointer to N u64 slots, writes its results there and returns 0:

    @export
    def my_parity(frames: Int32, out_ptr: UInt64) abi("C") -> Int32:
        var out = Pointer[UInt64, MutUntrackedOrigin](unsafe_from_address=Int(out_ptr))
        out[0] = ...; out[1] = ...
        return 0

    from ludens_wasm_rt import parity
    wasm = parity.wasm_u64s(Path("build/x.wasm"), "my_parity", 2, [240])            # host="node" (host.mjs)
    wasm = parity.wasm_u64s(Path("build/x.wasm"), "my_parity", 2, [240], host="wasmtime")
    ok = parity.compare(["world", "pcm"], native_values, wasm)                       # prints one line per value

    python -m ludens_wasm_rt.parity build/x.wasm my_parity 2 240                    # prints the values

The scenes stay in the consumer; this module only runs them and compares. Equality, not tolerance.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

from . import HOST_MJS


def wasm_u64s(wasm: Path, export: str, n: int, args: list[int] | tuple[int, ...] = (), host: str = "node") -> list[int]:
    """Call `export(*args, out)` with out = n u64 slots; raise if it does not return 0; return the n values."""
    if host == "node":
        js = f"""import({json.dumps(str(HOST_MJS))}).then(async m => {{
          const rt = await m.instantiate(require('fs').readFileSync({json.dumps(str(wasm))}));
          const p = rt.alloc({8 * n});
          const rc = rt.call({json.dumps(export)}, {', '.join(str(int(a)) for a in args)}{', ' if args else ''}p);
          if (rc !== 0) throw new Error({json.dumps(export)} + ' returned ' + rc);
          const v = new BigUint64Array(rt.mem(), Number(p), {n});
          console.log(Array.from(v, x => x.toString()).join(' '));
        }}).catch(e => {{ console.error(String(e)); process.exit(1); }})"""
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"parity: node {export}: {(r.stdout + r.stderr)[-1500:]}")
        return [int(x) for x in r.stdout.split()]
    if host == "wasmtime":
        from .wasmtime_host import Rt
        rt = Rt(Path(wasm))
        p = rt.alloc(8 * n)
        rc = rt.call(export, *[int(a) for a in args], p)
        if rc != 0:
            raise RuntimeError(f"parity: wasmtime {export} returned {rc}")
        return list(struct.unpack(f"<{n}Q", rt.read(p, 8 * n)))
    raise ValueError(f"parity: host must be 'node' or 'wasmtime', not {host!r}")


def compare(names: list[str], native: list[int], wasm: list[int], label: str = "parity") -> bool:
    """Print `name native N  wasm64 M  equal|DIFFERENT` per value and `<label>: PASS|FAIL`; True when all equal."""
    ok = len(native) == len(wasm) == len(names)
    width = max([len(n) for n in names] + [4])
    for name, a, b in zip(names, native, wasm):
        same = int(a) == int(b)
        ok &= same
        print(f"{name:{width}} native {a}  wasm64 {b}  {'equal' if same else 'DIFFERENT'}")
    if len(native) != len(wasm):
        print(f"{label}: native gave {len(native)} values, wasm64 {len(wasm)}")
    print(f"{label}:", "PASS" if ok else "FAIL")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wasm", type=Path)
    ap.add_argument("export")
    ap.add_argument("n", type=int, help="number of u64 results")
    ap.add_argument("args", nargs="*", type=int, help="scalar arguments before the result pointer")
    ap.add_argument("--host", default="node", choices=["node", "wasmtime"])
    a = ap.parse_args(argv)
    print(" ".join(str(v) for v in wasm_u64s(a.wasm, a.export, a.n, a.args, a.host)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
