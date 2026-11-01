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


NATIVE_KEYS = {"static_libs": list, "link_flags": list, "mojo_defines": list}


def _check(path: Path, table: str, data: dict, keys: dict) -> dict:
    out = data.get(table, {})
    for key, typ in keys.items():
        out.setdefault(key, typ())
        if not isinstance(out[key], typ):
            raise SystemExit(f"{path}: [{table}] {key} must be a {typ.__name__}")
    unknown = set(out) - set(keys)
    if unknown:
        raise SystemExit(f"{path}: unknown [{table}] keys: {sorted(unknown)}")
    return out


def load(path: Path) -> dict:
    """Return the [lib] table, with the optional [native] table under lib["native"]."""
    text = path.read_text()
    try:
        import tomllib
        data = tomllib.loads(text)
    except ImportError:
        data = _parse(text)  # noqa: pre-3.11 fallback
    if data.get("lib") is None or "name" not in data["lib"]:
        raise SystemExit(f"{path}: missing [lib] name")
    unknown = set(data) - {"lib", "native"}
    if unknown:
        raise SystemExit(f"{path}: unknown tables: {sorted(unknown)}")
    lib = _check(path, "lib", data, KEYS)
    lib["native"] = _check(path, "native", data, NATIVE_KEYS)
    return lib
