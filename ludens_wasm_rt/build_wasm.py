#!/usr/bin/env python3
"""Build one wasm64 module from Mojo entry code plus the C sources of wasm.toml libraries.

    python3 -m ludens_wasm_rt.build_wasm --lib path/to/lib [--lib other] \\
        entry.mojo -I extra/mojo/include -o build/wasm/app.wasm --export 'web_*' \\
        [--math musl|host] [--host js|wasi] [--target wasm64|native]

Each --lib directory holds a wasm.toml (see wasm_toml.py). Mojo 1.2.0 has no
WebAssembly target, so:
  1. mojo build --emit llvm (+ each lib's mojo_defines, --target-cpu x86-64)
  2. retarget.py   triple / datalayout swap
  3. opt -O3, llc -O3 (LLVM >= 23, wasm64 object)
  4. clang --target=wasm64: every lib's c_sources, the vendored musl subset in libc/,
     and runtime.c (the KGEN / libc runtime Mojo needs)
  5. wasm-ld -mwasm64: one module; the only imports are `env.host_*` from runtime.c
     plus whatever a library lists in host_imports.  `--host wasi` implements host_* on
     WASI preview 1 instead; `--math host` imports host_sinf / host_cosf (experimental).
  6. gates (audit.py): every import is the runtime's or a library's declared host_import, every
     library import signature is scalar, and memory / __indirect_function_table / mw_alloc /
     mw_free are exported.

`--target native` builds the same entry with `mojo build` against each lib's [native] section
(static_libs, link_flags, mojo_defines) instead.

Needs clang / llc / opt / wasm-ld / llvm-nm 23 on PATH. C objects are cached under
<out dir>/obj and rebuilt when a source or header is newer.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ludens_wasm_rt import audit, wasm_toml, wasmbin

HERE = Path(__file__).resolve().parent
LIBC = HERE / "libc"
# musl sources (libc/musl/src/<dir>/<name>.c) for what C libraries and Mojo call.
MUSL = [
    "math/exp", "math/exp_data", "math/log", "math/log_data", "math/pow", "math/pow_data",
    "math/sin", "math/cos", "math/__sin", "math/__cos", "math/__rem_pio2", "math/__rem_pio2_large",
    "math/sinf", "math/cosf", "math/__sindf", "math/__cosdf", "math/__rem_pio2f",
    "math/sqrt", "math/sqrt_data", "math/sqrtf", "math/floor", "math/floorf", "math/ceil", "math/ceilf",
    "math/fabs", "math/fabsf", "math/scalbn", "math/scalbnf", "math/fma", "math/fmaf", "math/fmod", "math/fmodf", "math/fminf", "math/fmaxf", "math/fmin", "math/fmax",
    "math/__math_oflow", "math/__math_uflow", "math/__math_xflow", "math/__math_invalid",
    "math/__math_divzero", "math/__math_oflowf", "math/__math_uflowf", "math/__math_xflowf",
    "math/__math_invalidf", "math/__math_divzerof",
    "string/memcpy", "string/memmove", "string/memset", "string/memcmp", "string/strlen",
    "string/strcmp", "string/strncmp", "string/strncpy", "string/strcpy", "string/strcasecmp",
    "string/wcslen", "ctype/tolower",
]
LLC_ATTRS = "+tail-call,+simd128,+bulk-memory,+nontrapping-fptoint,+sign-ext,+mutable-globals"
C_FEATURES = ["-mbulk-memory", "-mnontrapping-fptoint", "-msign-ext", "-mmutable-globals"]



REQUIRED_EXPORTS = ["memory", "__indirect_function_table", "mw_alloc", "mw_free"]
MATH_HOST_SKIP = {"math/sinf", "math/cosf"}


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        sys.exit(f"build_wasm: `{name}` not on PATH (needs LLVM 23: clang, opt, llc, llvm-nm, wasm-ld)")
    return path


def run(cmd: list[str], what: str, cwd: Path, env: dict | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.exit(f"build_wasm: {what} failed ({cmd[0]})\n" + (proc.stdout + proc.stderr)[-4000:])


class Lib:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cfg = wasm_toml.load(self.root / "wasm.toml")
        if self.cfg["js"] and not (self.root / self.cfg["js"]).is_file():
            sys.exit(f"build_wasm: {self.root / 'wasm.toml'}: js = {self.cfg['js']!r} does not exist")

    @property
    def name(self) -> str:
        return self.cfg["name"]

    def c_sources(self) -> list[Path]:
        out: list[Path] = []
        for pattern in self.cfg["c_sources"]:
            out += [Path(p) for p in sorted(glob.glob(str(self.root / pattern)))]
        return [p for p in out if p.name not in set(self.cfg["c_exclude"])]


def c_objects(clang: str, libs: list[Lib], obj_dir: Path, cwd: Path, host: str, math: str) -> list[Path]:
    base = [clang, "--target=wasm64-unknown-emscripten", "-nostdlibinc", f"-idirafter{LIBC / 'sysroot' / 'include'}",
            "-O2", "-ffp-contract=off", *C_FEATURES, "-w"]
    jobs: list[tuple[Path, list[str], list[Path], str]] = []
    for lib in libs:
        sources = lib.c_sources()
        dirs = sorted({p.parent for p in sources})
        flags = [f"-D{d}" for d in lib.cfg["c_defines"]] + [f"-I{d}" for d in dirs]
        headers = [h for d in dirs for h in d.glob("*.h")]
        jobs += [(src, flags, headers, "") for src in sources]
    runtime_flags = ["-ffreestanding"]
    tag = ""
    if host == "wasi":
        runtime_flags.append("-DWRT_HOST_WASI")
        tag += "_wasi"
    if math == "host":
        runtime_flags.append("-DWRT_MATH_HOST")
        tag += "_mathhost"
    jobs.append((HERE / "runtime.c", runtime_flags, [], tag))
    musl = LIBC / "musl"
    musl_flags = [f"-I{musl / 'src' / 'include'}", f"-I{musl / 'src' / 'internal'}",
                  f"-I{musl / 'arch' / 'emscripten'}", f"-I{musl / 'arch' / 'generic'}",
                  f"-I{musl / 'include'}", "-D_XOPEN_SOURCE=700", "-fno-builtin"]
    jobs += [(musl / "src" / f"{rel}.c", musl_flags, [], "") for rel in MUSL
             if not (math == "host" and rel in MATH_HOST_SKIP)]

    def build(job):
        src, flags, deps, tag = job
        # Key the cache by directory of origin so lib files of the same name do not collide.
        obj = obj_dir / (src.parent.name + "__" + src.stem + tag + ".o")
        newest = max(p.stat().st_mtime for p in [src, *deps])
        if not (obj.exists() and obj.stat().st_mtime >= newest):
            run([*base, *flags, "-c", str(src), "-o", str(obj)], f"compiling {src.name}", cwd)
        return obj

    obj_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor() as pool:
        return list(pool.map(build, jobs))


def mojo_cmd(entry: Path, includes: list[Path], libs: list[Lib], defines: list[str]) -> list[str]:
    inc = [str(lib.root / d) for lib in libs for d in lib.cfg["include"]] + [str(i) for i in includes]
    cmd = ["mojo", "build", "--target-cpu", "x86-64"]
    for d in defines:
        cmd += ["-D", d]
    for i in inc:
        cmd += ["-I", i]
    return cmd


def mojo_object(entry: Path, includes: list[Path], libs: list[Lib], out_dir: Path, cwd: Path) -> tuple[Path, Path]:
    ll = out_dir / (entry.stem + ".ll")
    wasm_ll = out_dir / (entry.stem + ".wasm64.ll")
    opt_ll = out_dir / (entry.stem + ".opt.bc")
    obj = out_dir / (entry.stem + ".mojo.o")
    cmd = mojo_cmd(entry, includes, libs, [d for lib in libs for d in lib.cfg["mojo_defines"]])
    cmd.insert(2, "--emit")
    cmd.insert(3, "llvm")
    run([*cmd, str(entry), "-o", str(ll)], "mojo --emit llvm", cwd)
    run([sys.executable, "-I", str(HERE / "retarget.py"), str(ll), str(wasm_ll)], "retarget", cwd)
    names = "-non-global-value-max-name-size=1000000"  # Mojo's mangled local names are long
    run([tool("opt"), names, "-O3", str(wasm_ll), "-o", str(opt_ll)], "opt -O3", cwd)
    run([tool("llc"), names, "-O3", "-filetype=obj", f"-mattr={LLC_ATTRS}", str(opt_ll), "-o", str(obj)], "llc", cwd)
    return obj, wasm_ll


def build_native(entry: Path, includes: list[Path], libs: list[Lib], out: Path, cwd: Path) -> int:
    defines = [d for lib in libs for d in lib.cfg["native"]["mojo_defines"]]
    cmd = mojo_cmd(entry, includes, libs, defines)
    for lib in libs:
        for path in lib.cfg["native"]["static_libs"]:
            archive = lib.root / path
            if not archive.is_file():
                sys.exit(f"build_wasm: {lib.root / 'wasm.toml'}: [native] static_libs {path!r} does not exist")
            cmd += ["-Xlinker", str(archive)]
        for flag in lib.cfg["native"]["link_flags"]:
            cmd += ["-Xlinker", flag]
    # The native link wants the system toolchain: a conda env that ships its own `ld` (the wasm
    # feature pulls binutils in with clang) has an older glibc sysroot that cannot link Mojo's runtime.
    env = dict(os.environ, PATH=os.pathsep.join(["/usr/bin", "/bin", os.environ.get("PATH", "")]))
    cmd[0] = shutil.which("mojo") or "mojo"
    run([*cmd, str(entry), "-o", str(out)], "mojo build (native)", cwd, env)
    print(f"build_wasm: {out} (native, {out.stat().st_size:,} bytes)")
    return 0


def run_audits(wasm: Path, ir: Path, libs: list[Lib], host: str, math: str) -> None:
    data = wasmbin.parse(wasm.read_bytes())
    allowed = set(audit.RUNTIME_IMPORTS)
    if math == "host":
        allowed |= audit.MATH_HOST_IMPORTS
    if host == "wasi":
        allowed -= audit.RUNTIME_IMPORTS
    patterns = {lib.name: lib.cfg["host_imports"] for lib in libs}
    problems = audit.declare_audit(data["imports"], patterns, allowed, wasi=host == "wasi")
    lib_imports = {name for mod, name, kind, _ in data["imports"]
                   if mod == "env" and not name.startswith("host_") and kind == "func"}
    problems += audit.signature_audit(ir.read_text(), lib_imports)
    for mod, name, kind, sig in data["imports"]:
        if sig and "v128" in sig[0] + sig[1]:
            problems.append(f"{mod}.{name}: v128 in the wasm signature {sig}")
    exported = {n for n, _ in data["exports"]}
    problems += [f"module does not export `{n}`" for n in REQUIRED_EXPORTS if n not in exported]
    if problems:
        sys.exit("build_wasm: audit failed:\n  " + "\n  ".join(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entry")
    ap.add_argument("--lib", action="append", default=[], help="directory holding a wasm.toml (repeatable)")
    ap.add_argument("-I", dest="includes", action="append", default=[], help="extra Mojo import path")
    ap.add_argument("-o", dest="out", required=True)
    ap.add_argument("--export", action="append", default=[], help="symbol or prefix glob, e.g. 'web_*'")
    ap.add_argument("--math", choices=["musl", "host"], default="musl",
                    help="musl (supported): sinf/cosf compiled in; host (experimental): import host_sinf/host_cosf")
    ap.add_argument("--host", choices=["js", "wasi"], default="js",
                    help="js: host_* are `env` imports for host.mjs; wasi: implemented on WASI preview 1")
    ap.add_argument("--target", choices=["wasm64", "native"], default="wasm64")
    args = ap.parse_args(argv)

    cwd = Path.cwd()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    libs = [Lib(Path(p)) for p in args.lib]
    entry = Path(args.entry).resolve()
    includes = [Path(i).resolve() for i in args.includes]
    if args.target == "native":
        return build_native(entry, includes, libs, out, cwd)

    clang = tool("clang")
    with ThreadPoolExecutor(2) as pool:
        c_future = pool.submit(c_objects, clang, libs, out.parent / "obj", cwd, args.host, args.math)
        mojo_obj, ir = mojo_object(entry, includes, libs, out.parent, cwd)
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
    # --allow-undefined turns unresolved symbols into `env` imports; run_audits then names every one
    # that neither the runtime nor a library claims.
    run([tool("wasm-ld"), "-mwasm64", "--no-entry", "--export-memory", "--export-table", "--allow-undefined",
         "--export=mw_alloc", "--export=mw_free", "--gc-sections", "-O2",
         "--stack-first", "-z", "stack-size=1048576", *flat,
         str(mojo_obj), *map(str, c_objs), "-o", str(out)], "wasm-ld", cwd)
    run_audits(out, ir, libs, args.host, args.math)
    try:
        shown = out.relative_to(cwd)
    except ValueError:
        shown = out
    print(f"build_wasm: {shown} ({out.stat().st_size:,} bytes, {len(flat)} exports, audits ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
