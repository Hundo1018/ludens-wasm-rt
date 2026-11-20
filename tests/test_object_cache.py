#!/usr/bin/env python3
"""The C object cache must notice every input of a compile, not only the source's mtime.

    pixi run test-cache

`c_objects` names each object after the source and reuses it when it is newer than the source and its headers. A
change of `c_defines` (or of the compiler flags) touches neither, so the old object used to be linked silently. This
builds a one-file library twice with different `c_defines` and requires the second object to carry the new value.
Needs clang (wasm64 target) and llvm-objdump only; no Mojo."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ludens_wasm_rt import build_wasm  # noqa: E402

failures = 0


def check(ok: bool, msg: str) -> None:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    failures += not ok


def write_lib(root: Path, define: str, header_value: str = "0") -> build_wasm.Lib:
    (root / "lib").mkdir(exist_ok=True)
    header, source = root / "lib/value.h", root / "lib/value.c"
    # rewritten only when the content differs: a rewrite would move the mtime and hide the bug under test
    if not header.exists() or header.read_text() != f"#define HEADER_VALUE {header_value}\n":
        header.write_text(f"#define HEADER_VALUE {header_value}\n")
    if not source.exists():
        source.write_text('#include "value.h"\nint answer(void) { return VALUE + HEADER_VALUE; }\n')
    (root / "wasm.toml").write_text(f'[lib]\nname = "cachetest"\nc_sources = ["lib/*.c"]\nc_defines = ["{define}"]\n')
    return build_wasm.Lib(root)


def constants(obj: Path) -> set[int]:
    """The immediate operands of the object's i32.const instructions."""
    text = subprocess.run(["llvm-objdump", "-d", str(obj)], capture_output=True, text=True, check=True).stdout
    return {int(line.split("i32.const")[1].split()[0]) for line in text.splitlines() if "i32.const" in line}


def build(lib: build_wasm.Lib, obj_dir: Path, cwd: Path) -> Path:
    objs = build_wasm.c_objects(build_wasm.tool("clang"), [lib], obj_dir, cwd, "js", "musl")
    return next(o for o in objs if o.name.startswith("lib__value"))


def main() -> int:
    if shutil.which("llvm-objdump") is None:
        print("SKIP test_object_cache: llvm-objdump not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "libroot"
        root.mkdir()
        obj_dir = Path(tmp) / "obj"
        first = build(write_lib(root, "VALUE=7"), obj_dir, root)
        check(7 in constants(first), "first build: the object carries VALUE=7")
        # same sources, different define, nothing newer than the object: only the cache key can tell
        second = build(write_lib(root, "VALUE=9"), obj_dir, root)
        check(9 in constants(second) and 7 not in constants(second), "VALUE=9 after VALUE=7 with the sources untouched: the new value is in the object that is linked")
        third = build(write_lib(root, "VALUE=9"), obj_dir, root)
        mtime = third.stat().st_mtime_ns
        fourth = build(write_lib(root, "VALUE=9"), obj_dir, root)
        check(third == fourth and fourth.stat().st_mtime_ns == mtime, "an unchanged build reuses the object (the cache still works)")
        # a changed header the source includes
        fifth = build(write_lib(root, "VALUE=9", header_value="100"), obj_dir, root)
        check(109 in constants(fifth) or 100 in constants(fifth), "a changed header invalidates the object")
    print(f"test_object_cache: {'FAIL' if failures else 'PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
