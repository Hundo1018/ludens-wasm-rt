"""ludens-wasm-rt: the wasm64 runtime and build driver shared by the ludens Mojo libraries."""

from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_MJS = HERE / "host.mjs"
