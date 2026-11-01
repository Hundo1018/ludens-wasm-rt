"""Read a library's wasm.toml (the manifest ludens-wasm-rt builds from).

The manifest is a flat `[lib]` table of strings and string arrays, so Python
without `tomllib` (< 3.11) is served by a small reader instead of a dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

KEYS = {
    "name": str, "mojo_defines": list, "include": list, "c_sources": list, "c_exclude": list,
    "c_defines": list, "js": str, "host_imports": list,
}


def _value(text: str):
    text = text.strip()
    if text.startswith("["):
        return re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    m = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', text)
    if not m:
        raise ValueError(f"unsupported TOML value: {text!r}")
    return m.group(1)


def _parse(text: str) -> dict:
    out: dict = {}
    table = out
    lines = iter(text.splitlines())
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            table = out.setdefault(m.group(1), {})
            continue
        key, _, rest = line.partition("=")
        rest = rest.strip()
        while rest.startswith("[") and "]" not in rest:  # multi-line array
            rest += " " + next(lines).split("#", 1)[0].strip()
        table[key.strip()] = _value(rest)
    return out


def load(path: Path) -> dict:
    text = path.read_text()
    try:
        import tomllib
        data = tomllib.loads(text)
    except ImportError:
        data = _parse(text)
    lib = data.get("lib")
    if lib is None or "name" not in lib:
        raise SystemExit(f"{path}: missing [lib] name")
    for key, typ in KEYS.items():
        lib.setdefault(key, typ())
        if not isinstance(lib[key], typ):
            raise SystemExit(f"{path}: [lib] {key} must be a {typ.__name__}")
    unknown = set(lib) - set(KEYS)
    if unknown:
        raise SystemExit(f"{path}: unknown [lib] keys: {sorted(unknown)}")
    return lib
