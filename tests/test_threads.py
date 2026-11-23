#!/usr/bin/env python3
"""`build_wasm --threads`: one module, one shared memory64, several instances (node Workers), bit-identical results.

    pixi run test-threads

Builds tests/threads_kernel.mojo with --threads, checks the module's memory import and exports, then runs
tests/threads_run.mjs: the same items on the main instance alone, on 1 Worker and on 2 Workers sharing the memory.
Every value must have the same bit pattern as the single-instance run, and the allocator's live bytes (each item
allocates and frees a List, so two Workers meet in the allocator's lock) must come back to where they were. Also checks
that the audit rejects a threads module audited as a default build. Skips (exit 0) when mojo, node or an LLVM tool is
not on PATH."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ludens_wasm_rt import audit, build_wasm, wasmbin  # noqa: E402

TESTS = ROOT / "tests"
ITEMS = 3000
failures = 0


def check(ok: bool, msg: str) -> None:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    failures += not ok


def main() -> int:
    missing = [t for t in ("mojo", "node", "clang", "opt", "llc", "wasm-ld", "llvm-nm") if shutil.which(t) is None]
    if missing:
        print(f"SKIP test_threads: not on PATH: {', '.join(missing)}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        wasm = Path(tmp) / "threads.wasm"
        proc = subprocess.run([sys.executable, "-m", "ludens_wasm_rt.build_wasm", str(TESTS / "threads_kernel.mojo"),
                               "--threads", "--export", "threads_*", "-o", str(wasm)],
                              cwd=ROOT, capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip()
        check(proc.returncode == 0, f"build threads_kernel --threads: {out.splitlines()[-1] if out else ''}")
        if proc.returncode != 0:
            print(out[-2000:])
            return 1
        data = wasm.read_bytes()
        mod = wasmbin.parse(data)
        exports = {n for n, _ in mod["exports"]}
        flags = mod["memory_imports"].get(("env", "memory"))
        check(flags is not None and flags & audit.MEMORY_SHARED and flags & audit.MEMORY_64,
              f"imports env.memory, shared and 64-bit (limits flags {flags})")
        check("memory" not in exports, "exports no memory")
        check({"__stack_pointer", "__heap_base", "mw_alloc", "mw_free"} <= exports,
              "exports __stack_pointer, __heap_base, mw_alloc, mw_free")
        check(b"__wasm_init_tls" not in data, "no thread-local storage (setting __stack_pointer is all a Worker needs)")
        check(audit.memory_audit(mod["memory_imports"], threads=True) == [], "memory audit (threads) passes")
        check(len(audit.memory_audit(mod["memory_imports"], threads=False)) == 1,
              "memory audit (default build) rejects the imported memory")

        res = subprocess.run(["node", str(TESTS / "threads_run.mjs"), str(wasm), str(ITEMS)],
                             cwd=ROOT, capture_output=True, text=True, timeout=300)
        check(res.returncode == 0, f"node threads_run.mjs exits 0{'' if res.returncode == 0 else ': ' + res.stderr[-800:]}")
        if res.returncode != 0:
            return 1
        r = json.loads(res.stdout.strip().splitlines()[-1])
        print(f"  {json.dumps(r)}")
        check(r["n"] == ITEMS and r["zeros"] == 0, f"single instance filled {ITEMS} values")
        check(r["differing_1_worker"] == 0, "1 Worker on the shared memory: bit-identical to the single instance")
        check(r["differing_2_workers"] == 0, "2 Workers on the shared memory: bit-identical to the single instance")
        check(r["live_after"] == r["live_before"] == r["live_after_serial"],
              f"allocator live bytes unchanged after concurrent alloc / free ({r['live_before']} -> {r['live_after']})")
        check(r["peak"] > r["live_before"], "the kernel allocated (peak above the starting live bytes)")
    print(f"test_threads: {'FAIL' if failures else 'PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
