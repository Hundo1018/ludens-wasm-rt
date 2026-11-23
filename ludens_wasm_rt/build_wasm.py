#!/usr/bin/env python3
"""Build one wasm64 module from Mojo entry code plus the C sources of wasm.toml libraries.

    python3 -m ludens_wasm_rt.build_wasm --lib path/to/lib [--lib other] \\
        entry.mojo -I extra/mojo/include -o build/wasm/app.wasm --export 'web_*' \\
        [--math musl|host] [--host js|wasi] [--target wasm64|native] [--threads]

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

`--threads` builds a shared-memory module instead: llc +atomics, C -matomics, and wasm-ld imports a shared
memory64 `env.memory` (max 4 GiB) and exports __stack_pointer / __heap_base, so one module can be instantiated once per
Worker on one memory. The audit then requires that import and no exported memory. See README "Threads".

`--target native` builds the same entry with `mojo build` against each lib's [native] section
(static_libs, link_flags, mojo_defines) instead.

Needs clang / llc / opt / wasm-ld / llvm-nm 23 on PATH. C objects are cached under
<out dir>/obj and rebuilt when a source or header is newer.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import shlex
import shutil
import re
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
# The wasm feature set of the whole pipeline: opt and llc both get LLC_ATTRS (opt's -mattr is what its vectorizers
# cost against: retarget.py strips the IR's own target-features, so without it opt -O3 plans for a target without
# simd128), and every C object gets C_FEATURES (clang's -msimd128 enables autovectorization of musl / runtime.c).
# C_FEATURES is part of each object's cache key, so a change here rebuilds the cached objects.
LLC_ATTRS = "+tail-call,+simd128,+bulk-memory,+nontrapping-fptoint,+sign-ext,+mutable-globals"
C_FEATURES = ["-msimd128", "-mbulk-memory", "-mnontrapping-fptoint", "-msign-ext", "-mmutable-globals"]



REQUIRED_EXPORTS = ["memory", "__indirect_function_table", "mw_alloc", "mw_free"]
# --threads: the memory is imported (shared), and __stack_pointer is exported so each extra instance gets its own stack.
THREADS_REQUIRED_EXPORTS = ["__indirect_function_table", "mw_alloc", "mw_free", "__stack_pointer"]
THREADS_LLC_ATTRS = ",+atomics"
THREADS_C_FEATURES = ["-matomics"]
# --max-memory of a threads build: a shared memory must declare a maximum. 4 GiB = 65536 pages.
THREADS_MAX_MEMORY = 4294967296
MATH_HOST_SKIP = {"math/sinf", "math/cosf"}


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        sys.exit(f"build_wasm: `{name}` not on PATH (needs LLVM 23: clang, opt, llc, llvm-nm, wasm-ld)")
    return path


_DIAG = re.compile(r"^(?:\S[^\n]*?): (error|warning|note|remark): ", re.M)
ERROR_LIMIT = 20
TAIL_CHARS = 4000


def failure_report(output: str) -> str:
    """What to show of a failed tool's output: its `error:` diagnostics first (each with the lines up to the next
    diagnostic, at most ERROR_LIMIT of them), or the last TAIL_CHARS characters when it has none.

    A tail alone hides the errors of a compiler that prints hundreds of warnings after them."""
    starts = [(m.start(), m.group(1)) for m in _DIAG.finditer(output)]
    errors = []
    for i, (pos, kind) in enumerate(starts):
        if kind != "error":
            continue
        end = len(output)
        for nxt, nkind in starts[i + 1:]:
            if nkind != "note":  # a note belongs to the diagnostic before it
                end = nxt
                break
        errors.append(output[pos:end].rstrip())
    if not errors:
        return output[-TAIL_CHARS:]
    shown = errors[:ERROR_LIMIT]
    head = f"{len(errors)} error(s)" + (f", first {ERROR_LIMIT} shown" if len(errors) > ERROR_LIMIT else "")
    return head + "\n\n" + "\n\n".join(shown)


def run(cmd: list[str], what: str, cwd: Path, env: dict | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.exit(f"build_wasm: {what} failed ({cmd[0]})\n" + failure_report(proc.stdout + proc.stderr))


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


def c_objects(clang: str, libs: list[Lib], obj_dir: Path, cwd: Path, host: str, math: str,
              threads: bool = False) -> list[Path]:
    # -matomics is part of `base`, so it is part of every object's cache key: a threads object is never linked into a
    # default build or the other way round. Every object needs it: wasm-ld --shared-memory refuses one without atomics.
    features = C_FEATURES + (THREADS_C_FEATURES if threads else [])
    base = [clang, "--target=wasm64-unknown-emscripten", "-nostdlibinc", f"-idirafter{LIBC / 'sysroot' / 'include'}",
            "-O2", "-ffp-contract=off", *features, "-w"]
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

    compiler = subprocess.run([clang, "--version"], capture_output=True, text=True).stdout

    def build(job):
        src, flags, deps, tag = job
        # The object's name carries a hash of everything that decides its content except the file contents (those are
        # covered by the mtime test below): the compiler, every flag and define, and the source's full path. Before,
        # the name was the parent directory and the stem, so a changed c_defines or flag linked the old object, and two
        # libraries with a same-named directory and file shared one.
        key = hashlib.sha256("\0".join([compiler, *base, *flags, str(src.resolve())]).encode()).hexdigest()[:12]
        obj = obj_dir / (src.parent.name + "__" + src.stem + tag + "-" + key + ".o")
        newest = max(p.stat().st_mtime for p in [src, *deps])
        if not (obj.exists() and obj.stat().st_mtime >= newest):
            run([*base, *flags, "-c", str(src), "-o", str(obj)], f"compiling {src.name}", cwd)
        return obj

    obj_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor() as pool:
        return list(pool.map(build, jobs))


# The Mojo compiler command: `mojo` on PATH unless --mojo / LUDENS_MOJO names another (split like a shell would, so a
# whole command such as `pixi run --manifest-path ../lib/pixi.toml mojo` works: the consumer's pinned compiler, the one
# its precompiled packages were made with).
MOJO: list[str] = ["mojo"]


# The Mojo release this driver is known to retarget correctly. retarget.py rewrites the compiler's textual IR and the
# runtime provides KGEN / AsyncRT symbols whose signatures are Mojo's private ones, so a new minor release can break
# the build silently. Anything else stops the build unless --allow-mojo-version (or LUDENS_ALLOW_MOJO_VERSION=1) is given.
SUPPORTED_MOJO = (1, 2)


def parse_mojo_version(text: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) from `mojo --version` output such as 'Mojo 1.2.0 (2016d0cd)', or None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check_mojo_version(allow: bool) -> str:
    """Stop unless the compiler is the supported release; returns the version line for the build log."""
    proc = subprocess.run([*MOJO, "--version"], capture_output=True, text=True)
    text = (proc.stdout + proc.stderr).strip()
    version = parse_mojo_version(text)
    if version is None or version[:2] != SUPPORTED_MOJO:
        found = text.splitlines()[0] if text else f"no output from `{' '.join(MOJO)} --version`"
        msg = (f"build_wasm: this driver supports Mojo {SUPPORTED_MOJO[0]}.{SUPPORTED_MOJO[1]}.x; found: {found}. "
               "The IR retarget and the runtime's KGEN symbols depend on the compiler release. "
               "Pass --allow-mojo-version to build anyway.")
        if not allow:
            sys.exit(msg)
        print(msg.replace("build_wasm:", "build_wasm: warning:").replace(" Pass --allow-mojo-version to build anyway.", ""), file=sys.stderr)
    return text.splitlines()[0] if text else "mojo (unknown version)"


def mojo_cmd(entry: Path, includes: list[Path], libs: list[Lib], defines: list[str], debug: str = "none") -> list[str]:
    inc = [str(lib.root / d) for lib in libs for d in lib.cfg["include"]] + [str(i) for i in includes]
    cmd = [*MOJO, "build", "--target-cpu", "x86-64"]
    if debug != "none":
        cmd += ["--debug-level", debug]
    for d in defines:
        cmd += ["-D", d]
    for i in inc:
        cmd += ["-I", i]
    return cmd


def mojo_object(entry: Path, includes: list[Path], libs: list[Lib], out_dir: Path, cwd: Path,
                debug: str = "none", threads: bool = False) -> tuple[Path, Path]:
    ll = out_dir / (entry.stem + ".ll")
    wasm_ll = out_dir / (entry.stem + ".wasm64.ll")
    opt_ll = out_dir / (entry.stem + ".opt.bc")
    obj = out_dir / (entry.stem + ".mojo.o")
    cmd = mojo_cmd(entry, includes, libs, [d for lib in libs for d in lib.cfg["mojo_defines"]], debug)
    at = len(MOJO) + 1  # after `build`
    cmd[at:at] = ["--emit", "llvm"]
    run([*cmd, str(entry), "-o", str(ll)], "mojo --emit llvm", cwd)
    run([sys.executable, "-I", str(HERE / "retarget.py"), str(ll), str(wasm_ll)], "retarget", cwd)
    names = "-non-global-value-max-name-size=1000000"  # Mojo's mangled local names are long
    attrs = LLC_ATTRS + (THREADS_LLC_ATTRS if threads else "")
    run([tool("opt"), names, "-O3", f"-mattr={attrs}", str(wasm_ll), "-o", str(opt_ll)], "opt -O3", cwd)
    run([tool("llc"), names, "-O3", "-filetype=obj", f"-mattr={attrs}", str(opt_ll), "-o", str(obj)], "llc", cwd)
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
    if len(MOJO) == 1:
        cmd[0] = shutil.which(MOJO[0]) or MOJO[0]
    run([*cmd, str(entry), "-o", str(out)], "mojo build (native)", cwd, env)
    print(f"build_wasm: {out} (native, {out.stat().st_size:,} bytes)")
    return 0


def run_audits(wasm: Path, ir: Path, libs: list[Lib], host: str, math: str, threads: bool = False) -> None:
    data = wasmbin.parse(wasm.read_bytes())
    allowed = set(audit.RUNTIME_IMPORTS)
    if math == "host":
        allowed |= audit.MATH_HOST_IMPORTS
    if host == "wasi":
        allowed -= audit.RUNTIME_IMPORTS
    patterns = {lib.name: lib.cfg["host_imports"] for lib in libs}
    # memory imports are judged by the memory audit alone (accepted only as a --threads build's shared env.memory)
    problems = audit.memory_audit(data["memory_imports"], threads)
    problems += audit.declare_audit([i for i in data["imports"] if i[2] != "memory"], patterns, allowed,
                                    wasi=host == "wasi")
    lib_imports = {name for mod, name, kind, _ in data["imports"]
                   if mod == "env" and not name.startswith("host_") and kind == "func"}
    problems += audit.signature_audit(ir.read_text(), lib_imports)
    for mod, name, kind, sig in data["imports"]:
        if sig and "v128" in sig[0] + sig[1]:
            problems.append(f"{mod}.{name}: v128 in the wasm signature {sig}")
    exported = {n for n, _ in data["exports"]}
    required = THREADS_REQUIRED_EXPORTS if threads else REQUIRED_EXPORTS
    problems += [f"module does not export `{n}`" for n in required if n not in exported]
    if threads and "memory" in exported:
        problems.append("a --threads build imports its memory and must not export one")
    if problems:
        sys.exit("build_wasm: audit failed:\n  " + "\n  ".join(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entry")
    ap.add_argument("--mojo", default=os.environ.get("LUDENS_MOJO", "mojo"), metavar="CMD",
                    help="the Mojo compiler command (default: LUDENS_MOJO, else `mojo` on PATH); split like a shell "
                         "command, e.g. --mojo 'pixi run --manifest-path ../lib/pixi.toml mojo'")
    ap.add_argument("--fma-report", type=int, default=0, metavar="N",
                    help="also list the N functions with the most fusable arithmetic (audit.fma_report)")
    ap.add_argument("--lib", action="append", default=[], help="directory holding a wasm.toml (repeatable)")
    ap.add_argument("-I", dest="includes", action="append", default=[], help="extra Mojo import path")
    ap.add_argument("-o", dest="out", required=True)
    ap.add_argument("--export", action="append", default=[], help="symbol or prefix glob, e.g. 'web_*'")
    ap.add_argument("--math", choices=["musl", "host"], default="musl",
                    help="musl (supported): sinf/cosf compiled in; host (experimental): import host_sinf/host_cosf")
    ap.add_argument("--host", choices=["js", "wasi"], default="js",
                    help="js: host_* are `env` imports for host.mjs; wasi: implemented on WASI preview 1")
    ap.add_argument("--target", choices=["wasm64", "native"], default="wasm64")
    ap.add_argument("--allow-mojo-version", action="store_true",
                    help="build with a Mojo release other than the supported one (a warning instead of an error)")
    ap.add_argument("--debug", choices=["none", "line-tables", "full"], default="none",
                    help="DWARF in the module (.debug_* sections). line-tables: addresses map to .mojo file:line and "
                         "the code is byte-identical to a build without it. full: also variables, but it changes the "
                         "generated code (measured), so do not compare its hashes with a release build")
    ap.add_argument("--threads", action="store_true",
                    help="shared-memory build: atomics on, the memory is imported as a shared memory64 `env.memory` "
                         "(max 4 GiB) and __stack_pointer / __heap_base are exported, so several instances (one per "
                         "Worker) can run on one memory. The host creates the memory; see README")
    args = ap.parse_args(argv)
    if args.threads and args.target == "native":
        ap.error("--threads is a wasm64 option; --target native has no shared wasm memory")
    if args.threads and args.host == "wasi":
        ap.error("--threads with --host wasi is not supported (the WASI host_write stages its iovec in one static "
                 "buffer, which concurrent instances would race on)")
    MOJO[:] = shlex.split(args.mojo)
    print(f"build_wasm: {check_mojo_version(args.allow_mojo_version or os.environ.get('LUDENS_ALLOW_MOJO_VERSION') == '1')}")

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
        c_future = pool.submit(c_objects, clang, libs, out.parent / "obj", cwd, args.host, args.math, args.threads)
        mojo_obj, ir = mojo_object(entry, includes, libs, out.parent, cwd, args.debug, args.threads)
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
    if args.threads:
        memory = ["--import-memory", "--shared-memory", f"--max-memory={THREADS_MAX_MEMORY}",
                  "--export=__stack_pointer", "--export=__heap_base"]
    else:
        memory = ["--export-memory"]
    run([tool("wasm-ld"), "-mwasm64", "--no-entry", *memory, "--export-table", "--allow-undefined",
         "--export=mw_alloc", "--export=mw_free", "--gc-sections", "-O2",
         "--stack-first", "-z", "stack-size=1048576", *flat,
         str(mojo_obj), *map(str, c_objs), "-o", str(out)], "wasm-ld", cwd)
    run_audits(out, ir, libs, args.host, args.math, args.threads)
    try:
        shown = out.relative_to(cwd)
    except ValueError:
        shown = out
    fr = audit.fma_report(ir.read_text())
    print(f"build_wasm: fma: {fr['pairs']} contractible mul+add pairs (fused by a native FMA build, never by wasm64), "
          f"{fr['fmuladd']} llvm.fmuladd, {fr['fma']} llvm.fma, in {len(fr['functions'])} functions")
    if args.fma_report:
        top = sorted(fr["functions"].items(), key=lambda kv: -sum(kv[1]))
        for fn, (pairs, fmuladd, fma) in top[:args.fma_report]:
            print(f"  {pairs:6} {fmuladd:4} {fma:4}  {fn[:140]}")
    print(f"build_wasm: {shown} ({out.stat().st_size:,} bytes, {len(flat)} exports, audits ok"
          + (", threads: imports a shared env.memory)" if args.threads else ")"))
    if args.debug == "full":
        print("build_wasm: --debug full changes the generated code; its hashes are not those of a release build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
