"""libwasmrt_math.a: the wasm build's musl sinf / cosf as native objects.

    python3 -m ludens_wasm_rt.native_math -o build/libwasmrt_math.a [--target aarch64-linux-gnu]

A library that wants its native reference build bit-identical to its wasm64 build links this and
calls wrt_sinf / wrt_cosf instead of the platform libm.  Symbols are renamed (wrt_*) so they never
interpose on libm; the internal helpers are localized.  fma / fmaf need no copy: a correctly
rounded fma is the one IEEE result, and glibc and musl agree on it bit for bit.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUSL = HERE / "libc" / "musl"
SOURCES = ["sinf", "cosf", "__sindf", "__cosdf", "__rem_pio2f", "__rem_pio2_large", "floor", "scalbn"]
EXPORTED = {"sinf": "wrt_sinf", "cosf": "wrt_cosf"}


def build(out: Path, target: str | None = None, cc: str | None = None) -> Path:
    cc = cc or shutil.which("clang") or sys.exit("native_math: clang not on PATH")
    objcopy = shutil.which("llvm-objcopy") or shutil.which("objcopy") or sys.exit("native_math: llvm-objcopy not found")
    ar = shutil.which("llvm-ar") or shutil.which("ar") or sys.exit("native_math: llvm-ar not found")
    ld = shutil.which("ld.lld") or shutil.which("ld")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        objs = []
        for name in SOURCES:
            obj = tmp / f"{name}.o"
            cmd = [cc, "-c", "-O2", "-ffp-contract=off", "-fno-builtin", "-fPIC", "-w", "-D_XOPEN_SOURCE=700",
                   # Same header set as the wasm build: musl's own LP64 headers, not the host libc's.
                   "-nostdlibinc", f"-I{MUSL / 'src' / 'include'}", f"-I{MUSL / 'src' / 'internal'}",
                   f"-I{MUSL / 'arch' / 'emscripten'}", f"-I{MUSL / 'arch' / 'generic'}", f"-I{MUSL / 'include'}",
                   f"-idirafter{HERE / 'libc' / 'sysroot' / 'include'}", str(MUSL / "src" / "math" / f"{name}.c"), "-o", str(obj)]
            if target:
                cmd.insert(1, f"--target={target}")
            subprocess.run(cmd, check=True)
            objs.append(obj)
        merged = tmp / "merged.o"
        subprocess.run([cc, "-r", "-nostdlib", "-fuse-ld=lld", *( [f"--target={target}"] if target else []),
                        *map(str, objs), "-o", str(merged)], check=True)
        renamed = tmp / "renamed.o"
        args = [objcopy]
        for old, new in EXPORTED.items():
            args += ["--redefine-sym", f"{old}={new}"]
        subprocess.run([*args, str(merged), str(renamed)], check=True)
        # Everything but wrt_* becomes local, so floor / scalbn here never interpose on libm.
        args = [objcopy]
        for new in EXPORTED.values():
            args += [f"--keep-global-symbol={new}"]
        subprocess.run([*args, str(renamed), str(tmp / "final.o")], check=True)
        out.unlink(missing_ok=True)
        subprocess.run([ar, "rcs", str(out), str(tmp / "final.o")], check=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", dest="out", required=True)
    ap.add_argument("--target", help="clang --target triple (default: this machine)")
    args = ap.parse_args(argv)
    print(f"native_math: {build(Path(args.out).resolve(), args.target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
