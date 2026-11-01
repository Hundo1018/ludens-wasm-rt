"""Build gates: every wasm import must be accounted for, and its signature must reach JS.

declare audit      each import of the linked module is `host_*` (the runtime) or matches a
                   library's `host_imports` pattern; anything else is a missing libc/Mojo
                   symbol and fails by name.
signature audit    each `declare` in the retargeted Mojo IR that ends up as a library import
                   takes only i32 / i64 / f32 / f64 / ptr and returns at most one such scalar.
                   v128, sret and by-value aggregates fail: JS imports cannot receive v128,
                   and Mojo lowers a by-value struct of <= 16 bytes containing a float to a
                   vector register.  ptr + len as two i64 is fine; a > 16 byte by-value struct
                   becomes a byval pointer, also fine.
"""

from __future__ import annotations

import fnmatch
import re

# Runtime-owned imports (runtime.c).  Anything `host_*` outside this set is rejected too.
RUNTIME_IMPORTS = {"host_write", "host_now_ns"}
MATH_HOST_IMPORTS = {"host_sinf", "host_cosf"}
WASI_IMPORTS = {"fd_write", "clock_time_get"}
SCALARS = {"i1", "i8", "i16", "i32", "i64", "float", "double", "ptr"}

_DECLARE = re.compile(r"^declare\s+(?P<head>.*?)@(?P<name>[^\s(]+)\((?P<params>.*)\)[^)]*$")


class AuditError(Exception):
    pass


def declare_audit(imports: list[tuple], libs: dict[str, list[str]], allowed_runtime: set[str],
                  wasi: bool = False) -> list[str]:
    """imports: wasmbin import tuples. libs: {library name: host_imports patterns}."""
    problems = []
    for mod, name, kind, _sig in imports:
        if wasi and mod == "wasi_snapshot_preview1":
            if name not in WASI_IMPORTS:
                problems.append(f"import {mod}.{name}: not in the supported WASI subset {sorted(WASI_IMPORTS)}")
            continue
        if mod != "env":
            problems.append(f"import {mod}.{name}: imports must live in module `env`")
        elif name.startswith("host_"):
            if name not in allowed_runtime:
                problems.append(f"import env.{name}: `host_*` is the runtime's prefix and `{name}` is not one of "
                                f"{sorted(allowed_runtime)}")
        elif not any(fnmatch.fnmatchcase(name, pat) for pats in libs.values() for pat in pats):
            problems.append(f"import env.{name}: undefined symbol not covered by the runtime or any library's "
                            f"host_imports (libraries: {sorted(libs) or 'none'})")
    return problems


def _split_params(params: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in params:
        if ch in "<{[(":
            depth += 1
        elif ch in ">}])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _param_type(param: str) -> str:
    """LLVM parameter text -> bare type, dropping attributes after the type."""
    m = re.match(r"(<[^>]*>|\{.*?\}|\[[^\]]*\]|[A-Za-z0-9_.%]+)", param)
    return m.group(1) if m else param


def signature_audit(ir_text: str, import_names: set[str]) -> list[str]:
    """Check the `declare` of every name in import_names found in the IR."""
    problems = []
    for line in ir_text.splitlines():
        if not line.startswith("declare "):
            continue
        m = _DECLARE.match(line)
        if not m or m.group("name").strip('"') not in import_names:
            continue
        name = m.group("name").strip('"')
        head = m.group("head")
        ret = [t for t in head.replace("noundef", "").replace("signext", "").replace("zeroext", "").split()
               if t not in {"dso_local", "hidden", "internal", "external", "nounwind", "fastcc", "ccc"}
               and not t.startswith("#")]
        ret_ty = ret[-1] if ret else "void"
        if ret_ty != "void" and ret_ty not in SCALARS:
            problems.append(f"{name}: return type `{ret_ty}` is not a scalar JS can receive")
        for p in _split_params(m.group("params")):
            ty = _param_type(p)
            if "sret" in p:
                problems.append(f"{name}: sret parameter `{p}` (aggregate return through memory)")
            elif ty not in SCALARS:
                problems.append(f"{name}: parameter `{p}` is `{ty}`, not i32/i64/f32/f64/ptr "
                                f"(vectors and by-value aggregates do not cross the JS boundary)")
    return problems
