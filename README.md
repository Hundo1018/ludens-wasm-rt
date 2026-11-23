# ludens-wasm-rt

Mojo 1.2.0 has no WebAssembly target. This repo supplies the missing piece so a Mojo package can be built to a
**wasm64** module: Modular's x86 host IR is retargeted, a minimal libc and Mojo runtime stubs are linked in,
several libraries' manifests are merged into one module, and a JS host composes imports and enforces the
re-entry rule. LLVM (`opt` / `llc` / `wasm-ld`) does the compiling.

It does not import or know any consumer (ludens-core, wgpu-mojo, miniaudio-mojo). The `ludens-` in the name
names the maintainer, not a dependency. Consumers depend on *it*, and only from a wasm-only pixi feature.

## Build

```
python3 -m ludens_wasm_rt.build_wasm [--lib path/to/lib]... entry.mojo [-I dir] -o out.wasm --export 'web_*'
    [--math musl|host] [--host js|wasi] [--target wasm64|native] [--debug none|line-tables|full] [--threads]
```

Pipeline: `mojo build --emit llvm --target-cpu x86-64` → `retarget.py` (wasm64 triple/datalayout, x86 attrs
removed, MXCSR intrinsics stubbed) → `opt -O3` → `llc` → `clang` for every C source, the vendored musl subset and
`runtime.c` → `wasm-ld -mwasm64 --export-table`. Only `wasm64` is supported: Mojo can lower 32-bit pointers, but
`opt -O3` output then traps (measured).

Toolchain (pinned in `pixi.toml`): LLVM 23.1.4 (`clang`, `llc`, `opt`, `wasm-ld`, `llvm-nm` must share a major
version, or `call_indirect` types disagree), Node ≥ 24 (memory64 / table64), wasmtime ≥ 50 (optional).
Feature set: `+tail-call,+simd128,+bulk-memory,+nontrapping-fptoint,+sign-ext,+mutable-globals`. relaxed-simd is
off because FMA contraction breaks bit-identity. Mojo flag: `--target-cpu x86-64` (host IR without FMA).

The compiler is `mojo` on `PATH`, or `--mojo CMD` / `LUDENS_MOJO` (split like a shell command), e.g.
`--mojo 'pixi run --manifest-path ../ludens-core/pixi.toml mojo'` to build with the library's pinned compiler, the one
its precompiled packages were made with.

## Debug info

`--debug line-tables` puts DWARF in the module (`.debug_info`, `.debug_line`, `.debug_str`, ...), so an address maps back
to a `.mojo` file and line (`llvm-dwarfdump --debug-line`, `llvm-symbolizer`). It does not touch the code: the code and
type sections are byte-identical to a build without it (checked in `tests/selftest.py`), so its hashes and behavior are
those of the release build. The cost is size: the 1,263-byte `hello_kernel` module becomes 95,181 bytes, nearly all of it
`.debug_str`; strip the sections for a shipped module (`llvm-strip --strip-debug`).

`--debug full` adds variable locations (`.debug_loc`) but changes the generated code (`hello_kernel`: code-section hash
`114d7d0b…` without, `a9f89a3e…` with), so do not compare its hashes with a release build. The driver says so when it is used.

The `name` section (function names for profilers and traces) is always present; `--debug` is not needed for it.

## Threads (`--threads`, shared memory)

`--threads` builds a module that several instances can run at once on one shared memory, one instance per Worker
(the same module, one memory, N instances; there is no `pthread_create` and the module starts no threads itself).
It is opt-in; a build without it is byte-identical to one made before the flag existed. What changes:

* `llc` gets `+atomics`, every C object (libraries, musl, `runtime.c`) `-matomics`; the flag is part of the object
  cache key, so threads and default objects never mix.
* `wasm-ld --import-memory --shared-memory --max-memory=4294967296 --export=__stack_pointer --export=__heap_base`
  instead of `--export-memory`: the module **imports** `env.memory` (shared, memory64, at most 65536 pages) and exports
  no memory.
* The audit requires exactly that import (shared and 64-bit flags set), `__stack_pointer` among the exports, and no
  exported memory; a default build still fails on any imported memory.
* `runtime.c`'s allocator (`malloc` / `free` / `mw_alloc` / `mw_free` and the Mojo runtime's aligned alloc) is
  serialized by one spin lock, compiled only under `__wasm_atomics__`. Nothing else in the runtime is made
  thread-safe (`errno` and stdio are shared), so `--threads --host wasi` is refused, as is `--target native`.

The host creates the memory and passes it to every instance; `initial` must be at least the module's declared
minimum (17 pages for `tests/threads_kernel.mojo`; it grows with the module's stack and data) and `maximum` at most
65536:

```js
const memory = new WebAssembly.Memory({ initial: 64n, maximum: 65536n, shared: true, address: 'i64' });
// node 24 also accepts `index: 'i64'` for the 64-bit key; try `address` first, then `index`
const env = { memory, host_write, host_now_ns };
const main = new WebAssembly.Instance(module, { env }).exports;   // the first instance initializes the data segments
const STACK = 1n << 20n;
const sp = main.mw_alloc(STACK, 16n) + STACK;                      // a stack grows down: pass its top
// in a Worker (module and memory are sent with postMessage / workerData):
const x = new WebAssembly.Instance(module, { env }).exports;
x.__stack_pointer.value = sp;                                      // before the first call into this instance
```

Every instance after the first must set `exports.__stack_pointer.value` to the top of its own stack region
(allocated with `mw_alloc`, 16-byte aligned) before calling anything, or it runs on the first instance's stack. The
modules built so far have no thread-local storage (`tests/test_threads.py` checks there is no `__wasm_init_tls`); a C
library that uses `_Thread_local` would also need `__wasm_init_tls` called per instance, which this build does not
support yet.

In a browser, `SharedArrayBuffer` (and so a shared `WebAssembly.Memory`) needs cross-origin isolation: serve the page
with `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` (check
`crossOriginIsolated`). The main thread may not `Atomics.wait`, so it must spin (or poll) for the Workers, or run its
share of the work loop itself. `host.mjs` and `wasmtime_host.py` read `exports.memory` and do not instantiate threads
builds yet; instantiate them as above.

`pixi run test-threads` builds `tests/threads_kernel.mojo` with `--threads` and runs it (`tests/threads_run.mjs`) on
the main instance alone, on 1 Worker and on 2 Workers sharing the memory: every value is bit-identical to the
single-instance run, and the allocator's live bytes come back to where they were (each item allocates and frees a
`List`). With the lock removed the 2-Worker run differs and the live bytes drift (checked by hand).

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
3. **memory audit**: a default build imports no memory; a `--threads` build imports exactly `env.memory`, shared and
   64-bit.
4. required exports present (`memory` for a default build; `__stack_pointer`, no `memory`, for `--threads`).

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

## Python host (`wasmtime_host.py`)

The same modules under wasmtime-py (memory64), for gates and tools that run wasm from Python:

```python
from ludens_wasm_rt.wasmtime_host import Rt
rt = Rt(Path("build/x.wasm"))              # host="js": env.host_* defined in Python; host="wasi": WASI preview 1
p = rt.alloc(16); rt.write(p, data); rt.call("my_export", p, 16); rt.read(p, 16)
```

Needs the `wasmtime` package (only importing this module does). `tests/wasmtime_host.py` re-exports it for consumers
that still put `tests/` on `sys.path`.

## Native-vs-wasm64 parity (`parity.py`)

A parity export writes N u64 results through a pointer and returns 0; `parity.wasm_u64s(wasm, export, n, args,
host="node"|"wasmtime")` runs it and `parity.compare(names, native, wasm)` prints the comparison. The scenes stay in
the consumer. `python -m ludens_wasm_rt.parity build/x.wasm export N [args...]` prints the values.

## Test

```
pixi run selftest
```

Builds the fixtures in `tests/` with the real driver and checks them under node, wasmtime and natively: hello kernel,
1e6-sample bit parity of `sinf` / `cosf` / `fmaf` / `fma` between wasm (node, wasmtime) and native, host.mjs and
re-entry, fnptr probe, both audits, the WASI host, and `--target native`. `pixi run test-threads` checks the
`--threads` build on node Workers (see Threads); `pixi run test-unit` the checks that need no Mojo.

## Packaging

`pixi add --git https://github.com/Hundo1018/ludens-wasm-rt --rev <commit> --pypi ludens-wasm-rt` inside a wasm-only
feature. `conda.recipe/` builds a noarch package (`pixi run check-recipe`).
