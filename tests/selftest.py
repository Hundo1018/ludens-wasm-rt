#!/usr/bin/env python3
"""ludens-wasm-rt selftest (`pixi run selftest`): every acceptance check that runs without a consumer.

Builds the fixtures under tests/ with the real driver and runs them under node (host.mjs), wasmtime
(wasmtime-py) and natively.  Prints PASS / FAIL lines; exit status is the number of failures.
"""

from __future__ import annotations

import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ludens_wasm_rt import audit, build_wasm, native_math, wasm_toml, wasmbin  # noqa: E402
from wasmtime_host import Rt  # noqa: E402

TESTS = ROOT / "tests"
BUILD = ROOT / "build" / "selftest"
SAMPLES = 1_000_000
failures = 0


def check(ok: bool, msg: str) -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    failures += not ok
    return ok


def driver(args: list[str], expect_ok: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ludens_wasm_rt.build_wasm", *args], cwd=ROOT,
                          capture_output=True, text=True)


def build(name: str, entry: str, *extra: str, out: str | None = None) -> Path | None:
    target = BUILD / (out or f"{name}.wasm")
    proc = driver([str(TESTS / entry), "-o", str(target), *extra])
    if not check(proc.returncode == 0, f"build {name}: {(proc.stdout.strip() or proc.stderr)[-300:]}"):
        return None
    return target


def node(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["node", *map(str, args)], cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)

    # ---- A1: hello kernel, native == node == wasmtime -------------------------------------------------
    native_hello = BUILD / "hello_native"
    proc = driver([str(TESTS / "hello_kernel.mojo"), "--target", "native", "-o", str(native_hello)])
    check(proc.returncode == 0, "build hello_kernel --target native")
    expected = subprocess.run([str(native_hello)], capture_output=True, text=True).stdout.strip()
    hello = build("hello", "hello_kernel.mojo", "--export", "hello_kernel")
    if hello:
        res = node("-e", f"""import('{ROOT}/ludens_wasm_rt/host.mjs').then(async m => {{
            const rt = await m.instantiate(require('fs').readFileSync('{hello}'));
            console.log(rt.call('hello_kernel', 100)); }})""")
        check(res.stdout.strip() == expected, f"hello_kernel(100): native {expected} == node {res.stdout.strip()}")
        wt = Rt(hello).call("hello_kernel", 100)
        check(str(wt) == expected, f"hello_kernel(100): native {expected} == wasmtime {wt}")

        # ---- --debug: DWARF in the module; line-tables leaves the code byte-identical -------------------------
        secs = {n or i: p for i, n, p in wasmbin.sections(hello.read_bytes())}
        check(not any(str(k).startswith(".debug_") for k in secs), "default build carries no DWARF")
        dbg = build("hello_dbg", "hello_kernel.mojo", "--export", "hello_kernel", "--debug", "line-tables", out="hello_dbg.wasm")
        if dbg:
            dsecs = {n or i: p for i, n, p in wasmbin.sections(dbg.read_bytes())}
            check(".debug_line" in dsecs and ".debug_info" in dsecs, "--debug line-tables: .debug_line and .debug_info present")
            check(dsecs[10] == secs[10] and dsecs[1] == secs[1], "--debug line-tables: code and type sections are byte-identical to the default build")
            lines = subprocess.run([build_wasm.tool("llvm-dwarfdump"), "--debug-line", str(dbg)], capture_output=True, text=True).stdout
            check("hello_kernel.mojo" in lines, "--debug line-tables: the line table names hello_kernel.mojo")
            check(str(Rt(dbg).call("hello_kernel", 100)) == expected, "--debug line-tables: hello_kernel(100) unchanged")
        full = build("hello_full", "hello_kernel.mojo", "--export", "hello_kernel", "--debug", "full", out="hello_full.wasm")
        if full:
            check(".debug_loc" in {n for _, n, _ in wasmbin.sections(full.read_bytes())}, "--debug full: variable locations present")
            check(str(Rt(full).call("hello_kernel", 100)) == expected, "--debug full: hello_kernel(100) unchanged")

    # ---- A2: libc ----------------------------------------------------------------------------------------
    math_wasm = build("math", "math_kernel.mojo", "--export", "k_*")
    if math_wasm:
        info = wasmbin.parse(math_wasm.read_bytes())
        check(all(m == "env" and n.startswith("host_") for m, n, *_ in info["imports"]),
              f"math.wasm imports are only env.host_*: {[n for _, n, *_ in info['imports']]}")
        exported = {n for n, _ in info["exports"]}
        check({"memory", "__indirect_function_table", "mw_alloc", "mw_free"} <= exported,
              "module exports memory, __indirect_function_table, mw_alloc, mw_free")
    lib = native_math.build(BUILD / "libwasmrt_math.a")
    syms = subprocess.run(["llvm-nm", "-g", "--defined-only", "-j", str(lib)], capture_output=True, text=True).stdout.split()
    und = subprocess.run(["llvm-nm", "-u", "-j", str(lib)], capture_output=True, text=True).stdout.split()
    syms, und = [s for s in syms if not s.endswith(":")], [s for s in und if not s.endswith(":")]
    check(sorted(syms) == ["wrt_cosf", "wrt_sinf"] and not und,
          f"libwasmrt_math.a exports only wrt_sinf / wrt_cosf and needs nothing: {syms} U={und}")
    if math_wasm:
        math_parity(math_wasm, lib)

    # ---- A4: host.mjs ------------------------------------------------------------------------------------
    echo = build("echo", "hello_kernel.mojo", "--lib", str(TESTS / "fixtures/echo"), "--export", "echo_*", out="echo.wasm")
    if hello and echo:
        res = node(TESTS / "test_host.mjs", hello, echo)
        print(res.stdout.rstrip())
        check(res.returncode == 0, f"tests/test_host.mjs {res.stderr[-300:]}")

    # ---- A5: gates --------------------------------------------------------------------------------------
    fnptr = build("fnptr", "fnptr_probe.mojo", "--lib", str(TESTS / "fixtures/fnptr"), "--export", "fnptr_*",
                  "--export", "call_int_cb", "--export", "slot_addr", "--export", "slot_status")
    if fnptr:
        res = node(TESTS / "fnptr_probe.mjs", fnptr)
        print(res.stdout.rstrip())
        check(res.returncode == 0, f"fnptr probe {res.stderr[-300:]}")
    gates()

    # ---- A6: WASI vs JS host -----------------------------------------------------------------------------
    probe_js = build("probe_js", "host_probe.mojo", "--export", "probe_*")
    probe_wasi = build("probe_wasi", "host_probe.mojo", "--host", "wasi", "--export", "probe_*", out="wasi/probe.wasm")
    if probe_js and probe_wasi:
        wi = wasmbin.parse(probe_wasi.read_bytes())["imports"]
        check({(m, n) for m, n, *_ in wi} == {("wasi_snapshot_preview1", "fd_write"), ("wasi_snapshot_preview1", "clock_time_get")},
              f"--host wasi imports only WASI preview 1: {[n for _, n, *_ in wi]}")
        rt = Rt(probe_js)
        rt.call("probe_print")
        check(bytes(rt.out) == b"hello from wasm\n", f"js host: host_write got {bytes(rt.out)!r}")
        outfile = BUILD / "wasi_stdout.txt"
        wasi = Rt(probe_wasi, host="wasi", stdout_file=str(outfile))
        wasi.call("probe_print")
        t1, t2 = wasi.call("probe_now"), wasi.call("probe_now")
        check(outfile.read_text() == "hello from wasm\n", f"wasmtime WASI: fd_write carried {outfile.read_text()!r}")
        check(0 < t1 <= t2, f"wasmtime WASI: clock_time_get advances ({t1} <= {t2})")
    if hello:
        wasi_hello = build("hello_wasi", "hello_kernel.mojo", "--host", "wasi", "--export", "hello_kernel", out="wasi/hello.wasm")
        if wasi_hello:
            check(Rt(wasi_hello, host="wasi").call("hello_kernel", 100) == int(expected), "WASI hello_kernel(100) == native")

    # ---- A8: --target native vs wasm64 from one manifest ----------------------------------------------
    native_target()

    # ---- wasm.toml ------------------------------------------------------------------------------------
    manifest_checks()

    print(f"\nselftest: {failures} failure(s)")
    return failures


def math_parity(math_wasm: Path, lib: Path) -> None:
    rng = random.Random(20261102)
    n = SAMPLES

    def f32s(count, wide):
        out = []
        while len(out) < count:
            if wide and rng.random() < 0.5:
                v = struct.unpack("<f", struct.pack("<I", rng.getrandbits(32)))[0]
                if v != v or v in (float("inf"), float("-inf")):
                    continue
            else:
                v = struct.unpack("<f", struct.pack("<f", rng.uniform(-1000, 1000)))[0]
            out.append(v)
        return out

    def f64s(count):
        out = []
        while len(out) < count:
            v = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
            if v == v and abs(v) != float("inf"):
                out.append(v)
        return out

    fa = []
    for wide in (True, False, False):
        fa.append(f32s(n, wide))
    arrays = [f32s(n, True), *fa[:1], *[f32s(n, False) for _ in range(2)]]
    arrays = [arrays[0], arrays[1], arrays[2], arrays[3]]
    blob = struct.pack("<II", n, 0)
    for a in arrays:
        blob += struct.pack(f"<{n}f", *a)
    for _ in range(3):
        blob += struct.pack(f"<{n}d", *f64s(n))
    inp = BUILD / "math_in.bin"
    inp.write_bytes(blob)

    harness = BUILD / "native_math_harness"
    cc = subprocess.run(["clang", "-O2", "-ffp-contract=off", str(TESTS / "native_math_harness.c"), str(lib), "-lm",
                         "-o", str(harness)], capture_output=True, text=True)
    if not check(cc.returncode == 0, f"native harness links libwasmrt_math.a {cc.stderr[-200:]}"):
        return
    nat_out = BUILD / "math_native.bin"
    subprocess.run([str(harness), str(inp), str(nat_out)], check=True)
    js_out = BUILD / "math_node.bin"
    res = node(TESTS / "math_run.mjs", math_wasm, inp, js_out)
    check(res.returncode == 0, f"node math run {res.stderr[-200:]}")

    rt = Rt(math_wasm)
    body = blob[8:]
    at = rt.alloc(len(body), 64)
    rt.write(at, body)
    f4, f8 = 4 * n, 8 * n
    out = rt.alloc(3 * f4 + f8, 64)
    sin_in, fa_, fb_, fc_ = at, at + f4, at + 2 * f4, at + 3 * f4
    da, db, dc = at + 4 * f4, at + 4 * f4 + f8, at + 4 * f4 + 2 * f8
    rt.call("k_sin_buf", sin_in, out, n)
    rt.call("k_cos_buf", sin_in, out + f4, n)
    rt.call("k_fmaf_buf", fa_, fb_, fc_, out + 2 * f4, n)
    rt.call("k_fma_buf", da, db, dc, out + 3 * f4, n)
    wt = rt.read(out, 3 * f4 + f8)

    nat, js = nat_out.read_bytes(), js_out.read_bytes()
    names = [("sinf", 0, f4), ("cosf", f4, f4), ("fmaf", 2 * f4, f4), ("fma", 3 * f4, f8)]
    for label, a, b in (("node", js, nat), ("wasmtime", wt, nat)):
        for fn, off, size in names:
            same = a[off:off + size] == b[off:off + size]
            check(same, f"{fn} over {n:,} samples: {label} == native bit for bit"
                        + ("" if same else f" ({sum(x != y for x, y in zip(a[off:off+size], b[off:off+size]))} bytes differ)"))


def gates() -> None:
    # declare audit, synthetic
    imports = [("env", "host_write", "func", None), ("env", "foo_bar", "func", None),
               ("env", "wgpuInstanceCreate", "func", None), ("env", "host_secret", "func", None)]
    problems = audit.declare_audit(imports, {"wgpu": ["wgpu*"]}, audit.RUNTIME_IMPORTS)
    check(any("foo_bar" in p for p in problems) and any("host_secret" in p for p in problems)
          and not any(p.startswith(("import env.wgpuInstanceCreate:", "import env.host_write:")) for p in problems),
          f"declare audit names foo_bar and host_secret only: {problems}")
    ir = ('declare void @bad_vec(<4 x float> noundef)\n'
          'declare i64 @ok_ptr_len(ptr noundef, i64 noundef, i64 noundef)\n'
          'declare void @bad_sret(ptr noalias sret({ i64, i64, i64 }) align 8)\n'
          'declare { i64, i64 } @bad_agg(i32 noundef)\n'
          'declare i32 @ok_byval(ptr byval({ i64, i64, i64 }) align 8, double noundef)\n')
    sp = audit.signature_audit(ir, {"bad_vec", "ok_ptr_len", "bad_sret", "bad_agg", "ok_byval"})
    flagged = {p.split(":")[0] for p in sp}
    check(flagged == {"bad_vec", "bad_sret", "bad_agg"}, f"signature audit flags v128 / sret / aggregate only: {sorted(flagged)}")

    # end to end: undeclared import fails the real build and is named
    tmp = Path(tempfile.mkdtemp(prefix="wrt_gate_"))
    try:
        (tmp / "undeclared.mojo").write_text(
            "from std.ffi import external_call\n\n\n@export\ndef run() abi(\"C\"):\n"
            "    external_call[\"foo_bar\", NoneType]()\n\n\ndef main():\n    pass\n")
        proc = driver([str(tmp / "undeclared.mojo"), "-o", str(tmp / "u.wasm"), "--export", "run"])
        check(proc.returncode != 0 and "foo_bar" in proc.stderr, f"undeclared import foo_bar fails the build and is named: {proc.stderr.strip()[-160:]}")

        lib = tmp / "badlib"
        lib.mkdir()
        (lib / "wasm.toml").write_text('[lib]\nname = "badlib"\nhost_imports = ["bad_*"]\n')
        (tmp / "vec.mojo").write_text(
            "from std.ffi import external_call\n\n\n@export\ndef run(x: Float32) abi(\"C\"):\n"
            "    external_call[\"bad_vec\", NoneType](SIMD[DType.float32, 4](x))\n\n\ndef main():\n    pass\n")
        proc = driver([str(tmp / "vec.mojo"), "--lib", str(lib), "-o", str(tmp / "v.wasm"), "--export", "run"])
        check(proc.returncode != 0 and "bad_vec" in proc.stderr and ("<4 x float>" in proc.stderr or "v128" in proc.stderr),
              f"v128 import bad_vec fails the build and is named: {proc.stderr.strip()[-200:]}")

        scalar = tmp / "scalar.mojo"
        scalar.write_text(
            "from std.ffi import external_call\n\n\n@export\ndef run(x: Int64) abi(\"C\"):\n"
            "    external_call[\"bad_ok\", NoneType](x, x)\n\n\ndef main():\n    pass\n")
        proc = driver([str(scalar), "--lib", str(lib), "-o", str(tmp / "s.wasm"), "--export", "run"])
        check(proc.returncode == 0, f"a declared scalar import (ptr/len as i64) passes: {proc.stderr.strip()[-200:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def native_target() -> None:
    fx = TESTS / "fixtures" / "native"
    (fx / "build").mkdir(exist_ok=True)
    subprocess.run(["clang", "-c", "-O2", str(fx / "nat.c"), "-o", str(fx / "build" / "nat.o")], check=True)
    subprocess.run(["llvm-ar", "rcs", str(fx / "build" / "libnat.a"), str(fx / "build" / "nat.o")], check=True)
    nat = BUILD / "native_entry"
    p1 = driver([str(TESTS / "native_entry.mojo"), "--lib", str(fx), "--target", "native", "-o", str(nat)])
    p2 = driver([str(TESTS / "native_entry.mojo"), "--lib", str(fx), "--target", "wasm64", "-o", str(BUILD / "native_entry.wasm"),
                 "--export", "entry_*"])
    check(p1.returncode == 0 and p2.returncode == 0, f"one manifest, two targets: {(p1.stderr + p2.stderr)[-300:]}")
    if p1.returncode == 0 and p2.returncode == 0:
        a = subprocess.run([str(nat)], capture_output=True, text=True).stdout.strip()
        b = Rt(BUILD / "native_entry.wasm").call("entry_sum", 20, 22)
        check(a == str(b) == "2022", f"entry_sum(20, 22): native {a} == wasm64 {b}")


def manifest_checks() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wrt_toml_"))
    try:
        (tmp / "wasm.toml").write_text('[lib]\nname = "x"\n[native]\nstatic_libs = ["a.a"]\nlink_flags = ["-lm"]\nmojo_defines = ["K=1"]\n')
        cfg = wasm_toml.load(tmp / "wasm.toml")
        check(cfg["native"] == {"static_libs": ["a.a"], "link_flags": ["-lm"], "mojo_defines": ["K=1"]}, "[native] section parsed")
        (tmp / "wasm.toml").write_text('[lib]\nname = "x"\n[toolchain]\nllvm = "23.1.4"\n')
        check(wasm_toml.load(tmp / "wasm.toml")["name"] == "x", "consumer-owned [toolchain] table is ignored")
        (tmp / "wasm.toml").write_text('[lib]\nname = "x"\n[bogus_table]\nk = 1\n')
        try:
            wasm_toml.load(tmp / "wasm.toml")
            check(False, "unknown table rejected")
        except SystemExit as e:
            check("bogus_table" in str(e), "unknown table rejected and named")
        (tmp / "wasm.toml").write_text('[lib]\nname = "x"\n[native]\nbogus = []\n')
        try:
            wasm_toml.load(tmp / "wasm.toml")
            check(False, "unknown [native] key rejected")
        except SystemExit as e:
            check("bogus" in str(e), "unknown [native] key rejected and named")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(min(main(), 125))
