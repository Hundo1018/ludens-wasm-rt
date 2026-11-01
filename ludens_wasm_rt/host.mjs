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
//    mid-allocation). rt.call() enforces it; rt.exports does not.

const decoder = new TextDecoder();
const encoder = new TextEncoder();

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
 * @param opts  { libs: [module | createImports fn], host: { write(fd, text) } }
 */
export async function instantiate(src, { libs = [], host = {} } = {}) {
  const write = host.write ?? defaultWrite;
  let inHost = 0;
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
    /** Call a wasm function through the indirect table (Mojo / C function pointers). */
    callIndirect(index, ...args) {
      if (inHost) throw new Error("re-entry: wasm called from inside a host import");
      return this.exports.__indirect_function_table.get(Number(index))(...args);
    },
    /** Call an export; refuses while a host import is running. */
    call(name, ...args) {
      if (inHost) throw new Error(`re-entry: ${name} called from inside a host import`);
      return this.exports[name](...args);
    },
  };

  const guard = (fn) => (...args) => {
    inHost++;
    try { return fn(...args); } finally { inHost--; }
  };
  const base = {
    env: { host_write: (fd, ptr, len) => write(Number(fd), rt.str(ptr, len)) },
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
