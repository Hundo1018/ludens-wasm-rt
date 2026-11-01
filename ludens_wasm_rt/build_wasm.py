#!/usr/bin/env python3
"""Build one wasm64 module from Mojo entry code plus the C sources of wasm.toml libraries.

    python3 -m ludens_wasm_rt.build_wasm --lib path/to/lib [--lib other] \\
        entry.mojo -I extra/mojo/include -o build/wasm/app.wasm --export 'web_*'

Each --lib directory holds a wasm.toml (see wasm_toml.py). Mojo 1.2.0 has no
WebAssembly target, so:
  1. mojo build --emit llvm (+ each lib's mojo_defines, --target-cpu x86-64)
  2. retarget.py   triple / datalayout swap
  3. opt -O3, llc -O3 (LLVM >= 23, wasm64 object)
  4. clang --target=wasm64: every lib's c_sources, the vendored musl subset in libc/,
     and runtime.c (the KGEN / libc runtime Mojo needs)
  5. wasm-ld -mwasm64: one module; the only imports are `env.host_*` from runtime.c
     plus whatever a library lists in host_imports.

Needs clang / llc / opt / wasm-ld / llvm-nm 23 on PATH. C objects are cached under
<out dir>/obj and rebuilt when a source or header is newer.
"""

from __future__ import annotations

import argparse
import glob
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ludens_wasm_rt import wasm_toml

HERE = Path(__file__).resolve().parent
LIBC = HERE / "libc"
# musl sources (libc/musl/src/<dir>/<name>.c) for what C libraries and Mojo call.
MUSL = [
    "math/exp", "math/exp_data", "math/log", "math/log_data", "math/pow", "math/pow_data",
    "math/sin", "math/cos", "math/__sin", "math/__cos", "math/__rem_pio2", "math/__rem_pio2_large",
    "math/sinf", "math/cosf", "math/__sindf", "math/__cosdf", "math/__rem_pio2f",
    "math/sqrt", "math/sqrt_data", "math/sqrtf", "math/floor", "math/floorf", "math/ceil", "math/ceilf",
    "math/fabs", "math/fabsf", "math/scalbn", "math/scalbnf", "math/fmod", "math/fmodf", "math/fminf", "math/fmaxf", "math/fmin", "math/fmax",
    "math/__math_oflow", "math/__math_uflow", "math/__math_xflow", "math/__math_invalid",
    "math/__math_divzero", "math/__math_oflowf", "math/__math_uflowf", "math/__math_xflowf",
    "math/__math_invalidf", "math/__math_divzerof",
    "string/memcpy", "string/memmove", "string/memset", "string/memcmp", "string/strlen",
    "string/strcmp", "string/strncmp", "string/strncpy", "string/strcpy", "string/strcasecmp",
    "string/wcslen", "ctype/tolower",
]
LLC_ATTRS = "+tail-call,+simd128,+bulk-memory,+nontrapping-fptoint,+sign-ext,+mutable-globals"
C_FEATURES = ["-mbulk-memory", "-mnontrapping-fptoint", "-msign-ext", "-mmutable-globals"]


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        sys.exit(f"build_wasm: `{name}` not on PATH (needs LLVM 23: clang, opt, llc, llvm-nm, wasm-ld)")
    return path


def run(cmd: list[str], what: str, cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"build_wasm: {what} failed ({cmd[0]})\n" + (proc.stdout + proc.stderr)[-4000:])


class Lib:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cfg = wasm_toml.load(self.root / "wasm.toml")

    def c_sources(self) -> list[Path]:
        out: list[Path] = []
        for pattern in self.cfg["c_sources"]:
            out += [Path(p) for p in sorted(glob.glob(str(self.root / pattern)))]
        return [p for p in out if p.name not in set(self.cfg["c_exclude"])]


def c_objects(clang: str, libs: list[Lib], obj_dir: Path, cwd: Path) -> list[Path]:
    base = [clang, "--target=wasm64-unknown-emscripten", "-nostdlibinc", f"-idirafter{LIBC / 'sysroot' / 'include'}",
            "-O2", "-ffp-contract=off", *C_FEATURES, "-w"]
    jobs: list[tuple[Path, list[str], list[Path]]] = []
    for lib in libs:
        sources = lib.c_sources()
        dirs = sorted({p.parent for p in sources})
        flags = [f"-D{d}" for d in lib.cfg["c_defines"]] + [f"-I{d}" for d in dirs]
        headers = [h for d in dirs for h in d.glob("*.h")]
        jobs += [(src, flags, headers) for src in sources]
    jobs.append((HERE / "runtime.c", ["-ffreestanding"], []))
    musl = LIBC / "musl"
    musl_flags = [f"-I{musl / 'src' / 'include'}", f"-I{musl / 'src' / 'internal'}",
                  f"-I{musl / 'arch' / 'emscripten'}", f"-I{musl / 'arch' / 'generic'}",
                  f"-I{musl / 'include'}", "-D_XOPEN_SOURCE=700", "-fno-builtin"]
    jobs += [(musl / "src" / f"{rel}.c", musl_flags, []) for rel in MUSL]

    def build(job):
        src, flags, deps = job
        obj = obj_dir / (src.parent.name + "__" + src.stem + ".o")
        newest = max(p.stat().st_mtime for p in [src, *deps])
        if not (obj.exists() and obj.stat().st_mtime >= newest):
            run([*base, *flags, "-c", str(src), "-o", str(obj)], f"compiling {src.name}", cwd)
        return obj

    obj_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor() as pool:
        return list(pool.map(build, jobs))


def mojo_object(entry: Path, includes: list[Path], libs: list[Lib], out_dir: Path, cwd: Path) -> Path:
    ll = out_dir / (entry.stem + ".ll")
    wasm_ll = out_dir / (entry.stem + ".wasm64.ll")
    opt_ll = out_dir / (entry.stem + ".opt.bc")
    obj = out_dir / (entry.stem + ".mojo.o")
    inc = [str(lib.root / d) for lib in libs for d in lib.cfg["include"]] + [str(i) for i in includes]
    defines = [d for lib in libs for d in lib.cfg["mojo_defines"]]
    cmd = ["mojo", "build", "--emit", "llvm", "--target-cpu", "x86-64"]
    for d in defines:
        cmd += ["-D", d]
    for i in inc:
        cmd += ["-I", i]
    run([*cmd, str(entry), "-o", str(ll)], "mojo --emit llvm", cwd)
    run([sys.executable, "-I", str(HERE / "retarget.py"), str(ll), str(wasm_ll)], "retarget", cwd)
    names = "-non-global-value-max-name-size=1000000"  # Mojo's mangled local names are long
    run([tool("opt"), names, "-O3", str(wasm_ll), "-o", str(opt_ll)], "opt -O3", cwd)
    run([tool("llc"), names, "-O3", "-filetype=obj", f"-mattr={LLC_ATTRS}", str(opt_ll), "-o", str(obj)], "llc", cwd)
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entry")
    ap.add_argument("--lib", action="append", default=[], help="directory holding a wasm.toml")
    ap.add_argument("-I", dest="includes", action="append", default=[], help="extra Mojo import path")
    ap.add_argument("-o", dest="out", required=True)
    ap.add_argument("--export", action="append", default=[], help="symbol or prefix glob, e.g. 'web_*'")
    args = ap.parse_args(argv)

    cwd = Path.cwd()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    libs = [Lib(Path(p)) for p in args.lib]
    clang = tool("clang")
    with ThreadPoolExecutor(2) as pool:
        c_future = pool.submit(c_objects, clang, libs, out.parent / "obj", cwd)
        mojo_obj = mojo_object(Path(args.entry).resolve(), [Path(i).resolve() for i in args.includes], libs, out.parent, cwd)
        c_objs = c_future.result()

    # wasm-ld has no export globs: expand them against the objects' defined symbols.
    names = subprocess.run([tool("llvm-nm"), "--defined-only", "-j", str(mojo_obj), *map(str, c_objs)],
                           capture_output=True, text=True).stdout.split()
    flat = []
    for e in args.export + ["rt_*"]:
        if "*" in e:
            prefix = e.rstrip("*")
            flat += [f"--export={n}" for n in sorted(set(names)) if n.startswith(prefix)]
        else:
            flat.append(f"--export={e}")
    run([tool("wasm-ld"), "-mwasm64", "--no-entry", "--export-memory", "--gc-sections", "-O2",
         "--stack-first", "-z", "stack-size=1048576", *flat,
         str(mojo_obj), *map(str, c_objs), "-o", str(out)], "wasm-ld", cwd)
    try:
        shown = out.relative_to(cwd)
    except ValueError:
        shown = out
    print(f"build_wasm: {shown} ({out.stat().st_size:,} bytes, {len(flat)} exports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
