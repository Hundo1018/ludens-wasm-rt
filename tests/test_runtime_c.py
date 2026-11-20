#!/usr/bin/env python3
"""runtime.c on its own: the C allocator's edge cases, under node. Needs clang, wasm-ld and node 24; no Mojo.

    pixi run test-runtime-c

runtime.c is compiled with the driver's own flags (`build_wasm.c_objects`) next to a one-file test, linked with wasm-ld
and run with the same host.mjs the consumers use."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ludens_wasm_rt import build_wasm  # noqa: E402

TEST_C = r"""
typedef unsigned long size_t;
void* calloc(size_t, size_t);
void free(void*);
/* runtime.c leaves memset / memcpy to musl; this test links without it */
void* memset(void* d, int c, size_t n) { unsigned char* p = d; while (n--) *p++ = (unsigned char)c; return d; }
void* memcpy(void* d, const void* s, size_t n) { unsigned char* p = d; const unsigned char* q = s; while (n--) *p++ = *q++; return d; }

/* calloc(count, size) where count * size wraps around 2^64 must fail instead of returning a small block. The
   arguments are volatile so the compiler cannot fold the call. Each export returns the pointer (0 = refused). */
unsigned long t_calloc_wrap_2_62_x_8(void) { volatile size_t a = (size_t)1 << 62, b = 8; return (unsigned long)calloc(a, b); }
unsigned long t_calloc_wrap_max_x_2(void) { volatile size_t a = (size_t)-1, b = 2; return (unsigned long)calloc(a, b); }
/* an ordinary calloc still returns zeroed memory */
unsigned long t_calloc_zeroed(void) {
    volatile size_t a = 100, b = 4;
    unsigned char* p = calloc(a, b);
    if (!p) return 0;
    for (int i = 0; i < 400; i++) if (p[i]) { free(p); return 0; }
    free(p);
    return 1;
}
/* a zero count or a zero size is not an overflow */
unsigned long t_calloc_zero_args(void) { volatile size_t a = 0, b = 8; return calloc(a, b) != 0 && calloc(b, a) != 0; }
"""

RUN_MJS = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [hostPath, wasm] = process.argv.slice(2);
const { instantiate } = await import(pathToFileURL(hostPath));
const rt = await instantiate(readFileSync(wasm));
let bad = 0;
const expect = { t_calloc_wrap_2_62_x_8: 0n, t_calloc_wrap_max_x_2: 0n, t_calloc_zeroed: 1n, t_calloc_zero_args: 1n };
for (const [name, want] of Object.entries(expect)) {
  const got = BigInt(rt.exports[name]());
  console.log(`${got === want ? 'PASS' : 'FAIL'} ${name}${got === want ? '' : ' returned ' + got}`);
  bad += got === want ? 0 : 1;
}
process.exit(bad ? 1 : 0);
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "t.c").write_text(TEST_C)
        (d / "run.mjs").write_text(RUN_MJS)
        clang = build_wasm.tool("clang")
        runtime = ROOT / "ludens_wasm_rt/runtime.c"
        objs = []
        for src, extra in ((runtime, ["-ffreestanding"]), (d / "t.c", [])):
            obj = d / (src.stem + ".o")
            subprocess.run([clang, "--target=wasm64-unknown-emscripten", "-nostdlibinc", "-O2", "-ffp-contract=off",
                            *build_wasm.C_FEATURES, "-w", "-fno-builtin", *extra, "-c", str(src), "-o", str(obj)], check=True)
            objs.append(str(obj))
        wasm = d / "t.wasm"
        subprocess.run([build_wasm.tool("wasm-ld"), "-mwasm64", "--no-entry", "--export-memory", "--export-table",
                        "--allow-undefined", "--export=mw_alloc", "--export=mw_free", "--export=t_calloc_wrap_2_62_x_8", "--export=t_calloc_wrap_max_x_2",
                        "--export=t_calloc_zeroed", "--export=t_calloc_zero_args", "--gc-sections", "--stack-first", "-z", "stack-size=1048576",
                        *objs, "-o", str(wasm)], check=True)
        host = ROOT / "ludens_wasm_rt/host.mjs"
        try:
            return subprocess.run(["node", str(d / "run.mjs"), str(host), str(wasm)], timeout=30).returncode
        except subprocess.TimeoutExpired:
            # the unchecked calloc(max, 2) asks block_alloc for 2^64 - 2 bytes, whose size-class loop never ends
            print("FAIL calloc did not return within 30 s (an unchecked product reached block_alloc)")
            return 1


if __name__ == "__main__":
    sys.exit(main())
