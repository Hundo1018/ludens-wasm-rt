"""Moved to ludens_wasm_rt/wasmtime_host.py (a public module). This file keeps `sys.path.insert(<rt>/tests);
from wasmtime_host import Rt` working for consumers that have not switched yet; new code imports the package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ludens_wasm_rt.wasmtime_host import Rt, _f32  # noqa: E402,F401
