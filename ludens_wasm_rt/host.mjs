// Shared JS runtime for ludens-wasm-rt modules (wasm64: memory64 + table64).
//
//   import { instantiate } from "./host.mjs";
//   import * as miniaudio from "./miniaudio_host.mjs";
//   const rt = await instantiate(fetch("app.wasm"), { libs: [miniaudio], host: {} });
//   rt.exports.web_create(48000);
//
// Conventions
//  - Pointers and sizes cross the boundary as BigInt (wasm64). rt.num(p) turns one into a
//    Number for typed-array views (safe below 2^53); rt.big(n) goes the other way.
//  - Memory can grow on any call into wasm, so never keep a view across a call: ask rt.bytes /
//    rt.f32 / rt.str again. rt.memory.buffer is always the current buffer.
//  - Libraries contribute imports as `createImports(rt)`, returning { namespace: { name: fn } }.
//    instantiate() merges them into one importObject and throws on a duplicate name.
//  - Re-entry rule: a host import must not call back into the module (the module may be
//    mid-allocation), and an asynchronous result (Promise callback) must not call into it either.
//    Async results go through rt.defer(fn) and are applied by rt.drain(), which a library's host
//    import calls synchronously when Mojo next enters it. rt.call() / rt.call_fn() enforce the
//    rule in debug mode (the default); rt.exports does not. Promise callbacks the runtime cannot
//    see are not caught: wrap them with rt.when(promise, fn) to have them checked.

const decoder = new TextDecoder();
const encoder = new TextEncoder();

const nowNs = () => BigInt(Math.round(performance.now() * 1e6));

function defaultWrite(fd, text) {
  if (typeof process !== "undefined" && process.stdout) (fd === 2 ? process.stderr : process.stdout).write(text);
  else console[fd === 2 ? "error" : "log"](text);
}

/** Merge importObject fragments; the same namespace.name from two sources is an error. */
export function mergeImports(...fragments) {
  const merged = {};
  for (const fragment of fragments) {
    for (const [ns, table] of Object.entries(fragment ?? {})) {
      const into = (merged[ns] ??= {});
      for (const [name, fn] of Object.entries(table)) {
        if (name in into) throw new Error(`import ${ns}.${name} provided twice`);
        into[name] = fn;
      }
    }
  }
  return merged;
}

async function compile(src, importObject) {
  if (src instanceof Promise) src = await src;
  if (typeof Response !== "undefined" && src instanceof Response) {
    if (WebAssembly.instantiateStreaming && src.headers.get("content-type") === "application/wasm") {
      return (await WebAssembly.instantiateStreaming(src, importObject)).instance;
    }
    src = await src.arrayBuffer();
  }
  return (await WebAssembly.instantiate(src, importObject)).instance;
}

/**
 * @param src   bytes (ArrayBuffer / Uint8Array), a Response, or a Promise of either
 * @param opts  { libs: [module | createImports fn], host: { write(fd, text), now() -> BigInt ns },
 *              debug: true }
 */
export async function instantiate(src, { libs = [], host = {}, debug = true } = {}) {
  const write = host.write ?? defaultWrite;
  const now = host.now ?? nowNs;
  let inHost = 0;
  let draining = 0;    // >0 while rt.drain() runs: the one place a callback may call into wasm
  let inAsync = null;  // label of the Promise continuation currently running (rt.when)
  const deferred = [];
  const rt = {
    instance: null,
    exports: null,
    get memory() { return this.exports.memory; },
    big: (n) => BigInt(n),
    num: (p) => Number(p),
    bytes(ptr, len) { return new Uint8Array(this.memory.buffer, Number(ptr), Number(len)); },
    f32(ptr, count) { return new Float32Array(this.memory.buffer, Number(ptr), Number(count)); },
    str(ptr, len) { return decoder.decode(this.bytes(ptr, len)); },
    /** Copy bytes into wasm memory at ptr. */
    put(ptr, data) { this.bytes(ptr, data.length).set(data); },
    encode: (text) => encoder.encode(text),
    /** The current ArrayBuffer of linear memory. Never keep it across a call into wasm. */
    mem() { return this.memory.buffer; },
    /** Copy a JS string into wasm memory (via the module's mw_alloc). Returns [ptr, len] as BigInt;
     *  release with rt.free(ptr). */
    str_in(text) {
      const data = encoder.encode(text);
      const ptr = this.exports.mw_alloc(BigInt(data.length + 1), 1n);
      this.put(ptr, data);
      this.bytes(ptr + BigInt(data.length), 1).fill(0);  // NUL after the text, for C callers
      return [ptr, BigInt(data.length)];
    },
    /** Read a UTF-8 string out of wasm memory. */
    str_out(ptr, len) { return this.str(ptr, len); },
    /** size bytes of wasm memory (the module's mw_alloc); release with rt.free. */
    alloc(size, align = 16) { return this.exports.mw_alloc(BigInt(size), BigInt(align)); },
    free(ptr) { this.exports.mw_free(ptr); },
    /** Throws, naming the entry point, when calling into wasm would violate the re-entry rule. */
    _entry(what) {
      if (inHost && !draining) throw new Error(`re-entry: ${what} called from inside a host import`);
      if (debug && inAsync) {
        throw new Error(`re-entry: ${what} called from the Promise callback '${inAsync}'; ` +
                        `queue it with rt.defer() and apply it with rt.drain()`);
      }
    },
    /** The function behind a table index (table64 wants a BigInt index, table32 a Number). */
    fn(index) {
      const table = this.exports.__indirect_function_table;
      try { return table.get(BigInt(index)); } catch (e) { if (!(e instanceof TypeError)) throw e; }
      return table.get(Number(index));
    },
    /** Call a wasm callback through the indirect table (Mojo / C function pointers: a c_fn_ptr is a
     *  table index under wasm64). Legal only inside rt.drain(); use rt.fn(index)(...) for a raw call
     *  in tests. Arguments and results follow wasm types: i64 / pointers are BigInt. */
    call_fn(index, ...args) {
      // A callback is invoked while Mojo is inside a host import that drains deferred results;
      // from a microtask or top-level JS it would re-enter wasm uninvited.
      if (!draining) {
        throw new Error(`re-entry: call_fn(${index}) outside rt.drain(); table callbacks run only while ` +
                        `Mojo is inside a host import (queue with rt.defer(), apply with rt.drain())`);
      }
      this._entry(`call_fn(${index})`);
      return this.fn(index)(...args);
    },
    /** Call an export; refuses while a host import or a tracked Promise callback is running. */
    call(name, ...args) {
      this._entry(name);
      return this.exports[name](...args);
    },
    /** Queue work that must not touch wasm now (typically an async result to be written into memory). */
    defer(fn) { deferred.push(fn); },
    /** Run queued work in order. A host import calls this when Mojo enters it, the one point where an
     *  async result may be applied synchronously; deferred work may call rt.call / rt.call_fn (a C
     *  callback through the table) here and only here. Returns how many ran. */
    drain() {
      let ran = 0;
      draining++;
      try { while (deferred.length) { deferred.shift()(); ran++; } } finally { draining--; }
      return ran;
    },
    /** promise.then(fn) with fn tracked: calling into wasm from inside it throws (debug mode). */
    when(promise, fn, label = fn.name || "anonymous") {
      return promise.then((value) => {
        const before = inAsync;
        inAsync = label;
        try { return fn(value); } finally { inAsync = before; }
      });
    },
  };
  rt.callIndirect = rt.call_fn;  // earlier name

  const guard = (fn) => (...args) => {
    inHost++;
    try { return fn(...args); } finally { inHost--; }
  };
  const base = {
    env: {
      host_write: (fd, ptr, len) => write(Number(fd), rt.str(ptr, len)),
      host_now_ns: () => BigInt.asUintN(64, BigInt(now())),
      // --math host (experimental): the host's libm instead of the vendored musl one.
      host_sinf: (x) => Math.fround(Math.sin(x)),
      host_cosf: (x) => Math.fround(Math.cos(x)),
    },
  };
  const fragments = [base];
  for (const lib of libs) {
    const create = typeof lib === "function" ? lib : lib.createImports;
    if (typeof create !== "function") throw new Error("lib must be a createImports function or export one");
    fragments.push(create(rt));
  }
  const importObject = mergeImports(...fragments);
  for (const table of Object.values(importObject)) {
    for (const [name, fn] of Object.entries(table)) table[name] = guard(fn);
  }

  rt.instance = await compile(src, importObject);
  rt.exports = rt.instance.exports;
  return rt;
}
