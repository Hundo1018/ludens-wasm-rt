"""A minimal wasmtime host for the same modules host.mjs runs (wasmtime-py, memory64)."""

from __future__ import annotations

import math
import struct
import time
from pathlib import Path

from wasmtime import Config, Engine, Func, FuncType, Linker, Module, Store, ValType, WasiConfig


class Rt:
    """host='js' wires the env.host_* imports in Python; host='wasi' uses wasmtime's WASI preview 1."""

    def __init__(self, path: Path, host: str = "js", stdout_file: str | None = None):
        cfg = Config()
        cfg.wasm_memory64 = True
        self.engine = Engine(cfg)
        self.store = Store(self.engine)
        self.out = bytearray()
        linker = Linker(self.engine)
        module = Module.from_file(self.engine, str(path))
        if host == "wasi":
            wasi = WasiConfig()
            if stdout_file:
                wasi.stdout_file = stdout_file
            wasi.inherit_stderr()
            self.store.set_wasi(wasi)
            linker.define_wasi()
        else:
            self._define_env(linker, module)
        self.instance = linker.instantiate(self.store, module)
        self.exports = self.instance.exports(self.store)

    def _define_env(self, linker: Linker, module: Module) -> None:
        i32, i64, f32 = ValType.i32(), ValType.i64(), ValType.f32()

        def write(caller, fd, ptr, n):
            self.out += self.read(ptr, n)

        funcs = {
            "host_write": Func(self.store, FuncType([i32, i64, i64], []), write, access_caller=True),
            "host_now_ns": Func(self.store, FuncType([], [i64]), lambda: time.monotonic_ns() & (2**63 - 1)),
            "host_sinf": Func(self.store, FuncType([f32], [f32]), lambda x: _f32(math.sin(x))),
            "host_cosf": Func(self.store, FuncType([f32], [f32]), lambda x: _f32(math.cos(x))),
        }
        for imp in module.imports:
            if imp.module != "env" or imp.name not in funcs:
                raise RuntimeError(f"unexpected import {imp.module}.{imp.name}")
            linker.define(self.store, "env", imp.name, funcs[imp.name])

    @property
    def memory(self):
        return self.exports["memory"]

    def call(self, name: str, *args):
        return self.exports[name](self.store, *args)

    def read(self, ptr: int, n: int) -> bytes:
        return self.memory.read(self.store, int(ptr), int(ptr) + int(n))

    def write(self, ptr: int, data: bytes) -> None:
        self.memory.write(self.store, data, int(ptr))

    def alloc(self, size: int, align: int = 16) -> int:
        return self.call("mw_alloc", size, align)


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]
