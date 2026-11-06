# ludens-wasm-rt

Mojo 1.2.0 has no WebAssembly target. This repo supplies the missing piece so a Mojo package can be built to a
**wasm64** module: Modular's x86 host IR is retargeted, a minimal libc and Mojo runtime stubs are linked in,
several libraries' manifests are merged into one module, and a JS host composes imports and enforces the
re-entry rule. LLVM (`opt` / `llc` / `wasm-ld`) does the compiling.

It does not import or know any consumer (ludens-engine, wgpu-mojo, miniaudio-mojo). The `ludens-` in the name
names the maintainer, not a dependency. Consumers depend on *it*, and only from a wasm-only pixi feature.

## Build

```
python3 -m ludens_wasm_rt.build_wasm [--lib path/to/lib]... entry.mojo [-I dir] -o out.wasm --export 'web_*'
    [--math musl|host] [--host js|wasi] [--target wasm64|native] [--debug none|line-tables|full]
```

Pipeline: `mojo build --emit llvm --target-cpu x86-64` → `retarget.py` (wasm64 triple/datalayout, x86 attrs
removed, MXCSR intrinsics stubbed) → `opt -O3` → `llc` → `clang` for every C source, the vendored musl subset and
`runtime.c` → `wasm-ld -mwasm64 --export-table`. Only `wasm64` is supported: Mojo can lower 32-bit pointers, but
`opt -O3` output then traps (measured).

Toolchain (pinned in `pixi.toml`): LLVM 23.1.4 (`clang`, `llc`, `opt`, `wasm-ld`, `llvm-nm` must share a major
version, or `call_indirect` types disagree), Node ≥ 24 (memory64 / table64), wasmtime ≥ 50 (optional).
Feature set: `+tail-call,+simd128,+bulk-memory,+nontrapping-fptoint,+sign-ext,+mutable-globals`. relaxed-simd is
off because FMA contraction breaks bit-identity. Mojo flag: `--target-cpu x86-64` (host IR without FMA).

## Debug info

`--debug line-tables` puts DWARF in the module (`.debug_info`, `.debug_line`, `.debug_str`, ...), so an address maps back
to a `.mojo` file and line (`llvm-dwarfdump --debug-line`, `llvm-symbolizer`). It does not touch the code: the code and
type sections are byte-identical to a build without it (checked in `tests/selftest.py`), so its hashes and behavior are
those of the release build. The cost is size: the 1,263-byte `hello_kernel` module becomes 95,181 bytes, nearly all of it
`.debug_str`; strip the sections for a shipped module (`llvm-strip --strip-debug`).

`--debug full` adds variable locations (`.debug_loc`) but changes the generated code (`hello_kernel`: code-section hash
`114d7d0b…` without, `a9f89a3e…` with), so do not compare its hashes with a release build. The driver says so when it is used.

The `name` section (function names for profilers and traces) is always present; `--debug` is not needed for it.

## wasm.toml

Each consumer library keeps a passive manifest at its root; the driver collects every `--lib`.

```toml
[lib]
name = "miniaudio"
mojo_defines = ["MINIAUDIO_LINK=static"]
include = ["src"]
c_sources = ["vendor/miniaudio/miniaudio.c", "src/native/ma_shim*.c"]
c_exclude = ["ma_shim_context.c"]
c_defines = ["MA_NO_DEVICE_IO"]
js = "web/miniaudio_host.mjs"      # optional; exports createImports(rt) -> { env: {...} }
host_imports = []                  # import names this library's JS provides (wgpu: ["wgpu*"])

[native]                           # used only by --target native
static_libs = ["build/libwgpu_native.a"]
link_flags = []
mojo_defines = ["WGPU_LINK=static"]
```

Imports all live in `env`. `host_*` belongs to the runtime; a library may only provide names it declares in
`host_imports`. The module exports `memory`, `__indirect_function_table`, `mw_alloc`, `mw_free` and the app's
`--export`s. For users who never build wasm the manifest does nothing.

`--target native` builds the same entry with `mojo build`, applying each lib's `[native]` section instead, so
a native reference build and the wasm64 build come from one manifest.

## Runtime and libc (`runtime.c`, `libc/`)

* vendored musl subset (MIT, `libc/musl/COPYRIGHT`): math, string, ctype. No emsdk needed.
* `write` → `host_write(fd, ptr, len)`, `clock_gettime` → `host_now_ns()`, `abort` → `host_write` + trap.
* `mw_alloc(size, align)` / `mw_free(ptr)` exported (wrap `KGEN_CompilerRT_AlignedAlloc`).
* Math is always compiled into the module from musl, including `fma` / `fmaf` (software, correctly rounded). `--math
  host` imports `host_sinf` / `host_cosf` instead and is experimental.
* `libwasmrt_math.a` (`python3 -m ludens_wasm_rt.native_math -o libwasmrt_math.a [--target …]`): the same musl
  `sinf` / `cosf` as native objects, symbols `wrt_sinf` / `wrt_cosf`, for native builds that must match wasm bit for
  bit.
* `--host wasi` implements `host_write` / `host_now_ns` on WASI preview 1 (`fd_write`, `clock_time_get`), so one
  module also runs under wasmtime.

## Gates (run on every build, `audit.py`)

1. **declare audit**: every import of the linked module is the runtime's or matches a library's `host_imports`;
   anything else fails and is named.
2. **signature audit**: each library import in the Mojo IR takes only `i32 / i64 / f32 / f64 / ptr` and returns at
   most one scalar. `v128`, `sret` and by-value aggregates fail (JS cannot receive v128, and Mojo lowers a ≤16-byte
   by-value struct containing a float to vector registers; `ptr + len` as two `i64` and >16-byte `byval` pointers
   are fine).
3. required exports present.

## JS host (`host.mjs`)

```js
import { instantiate } from "ludens_wasm_rt/host.mjs";
const rt = await instantiate(fetch("app.wasm"), { libs: [createImports], host: { write, now } });
```

`rt.mem()`, `rt.str_in(s) → [ptr, len]`, `rt.str_out(ptr, len)`, `rt.call_fn(idx, …)` (table call; `c_fn_ptr` is a
table index under wasm64), `rt.call(name, …)`. wasm64 pointers and `i64` are always `BigInt`. **Re-entry rule**: JS
must not call into wasm from inside a host import or from a Promise callback. Async results go through `rt.defer(fn)`
and are applied by `rt.drain()` when Mojo next enters a host import. In debug mode (default) `rt.call` / `rt.call_fn`
throw, naming the offender; wrap Promise callbacks with `rt.when(promise, fn)` to have them checked.

## Test

```
pixi run selftest
```

Builds the fixtures in `tests/` with the real driver and checks them under node, wasmtime and natively: hello kernel,
1e6-sample bit parity of `sinf` / `cosf` / `fmaf` / `fma` between wasm (node, wasmtime) and native, host.mjs and
re-entry, fnptr probe, both audits, the WASI host, and `--target native`.

## Packaging

`pixi add --git https://github.com/Hundo1018/ludens-wasm-rt --rev <commit> --pypi ludens-wasm-rt` inside a wasm-only
feature. `conda.recipe/` builds a noarch package (`pixi run check-recipe`).
