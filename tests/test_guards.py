#!/usr/bin/env python3
"""Build-gate behaviour that needs neither Mojo nor a toolchain: the Mojo version guard and the signature audit's
handling of a `declare` line it cannot parse.

    pixi run test-guards"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ludens_wasm_rt import audit, build_wasm  # noqa: E402

failures = 0


def check(ok: bool, msg: str) -> None:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    failures += not ok


def exits(fn) -> str | None:
    try:
        fn()
    except SystemExit as e:
        return str(e.code)
    return None


# --- Mojo version -------------------------------------------------------------------------------------------------
v = build_wasm.parse_mojo_version
check(v("Mojo 1.2.0 (2016d0cd)") == (1, 2, 0), "parses 'Mojo 1.2.0 (2016d0cd)'")
check(v("mojo 1.3.0.dev2026112005") == (1, 3, 0), "parses a dev release as its release number")
check(v("no digits here") is None, "no version in the output -> None")

saved = list(build_wasm.MOJO)
try:
    fake = lambda text: [sys.executable, "-c", f"print({text!r})"]
    build_wasm.MOJO[:] = fake("Mojo 1.2.4 (abcd)")
    check(exits(lambda: build_wasm.check_mojo_version(False)) is None, "Mojo 1.2.x passes")
    build_wasm.MOJO[:] = fake("Mojo 1.3.0 (abcd)")
    msg = exits(lambda: build_wasm.check_mojo_version(False))
    check(msg is not None and "1.3.0" in msg and "--allow-mojo-version" in msg, "Mojo 1.3.0 stops the build and names the override")
    check(exits(lambda: build_wasm.check_mojo_version(True)) is None, "--allow-mojo-version lets 1.3.0 through (with a warning)")
    build_wasm.MOJO[:] = fake("")
    check(exits(lambda: build_wasm.check_mojo_version(False)) is not None, "a compiler that prints no version stops the build")
finally:
    build_wasm.MOJO[:] = saved

# --- the signature audit fails closed -------------------------------------------------------------------------------
good = 'declare i32 @net_poll(i64 noundef, i64 noundef)'
check(audit.signature_audit(good, {"net_poll"}) == [], "a readable scalar declare passes")
bad_vec = 'declare i32 @net_poll(<4 x float> noundef)'
check(len(audit.signature_audit(bad_vec, {"net_poll"})) == 1, "a vector parameter is reported")
# the form the regex does not match: no closing parenthesis on the line (a long attribute list on the next line, say)
odd = 'declare i32 @net_poll(i64 noundef, <4 x float> noundef'
problems = audit.signature_audit(odd, {"net_poll"})
check(len(problems) == 1 and "net_poll" in problems[0] and "unchecked" in problems[0], "an unparseable declare of an import is reported, not skipped")
check(audit.signature_audit(odd, {"other_import"}) == [], "an unparseable declare of something that is not an import is still ignored")

print(f"test_guards: {'FAIL' if failures else 'PASS'}")
sys.exit(1 if failures else 0)
