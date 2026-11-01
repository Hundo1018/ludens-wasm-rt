// Drives fnptr_probe.wasm. Node >= 24 (table64). Usage: node fnptr_probe.mjs fnptr_probe.wasm
import { readFileSync } from "node:fs";
import { instantiate } from "../ludens_wasm_rt/host.mjs";

const rt = await instantiate(readFileSync(process.argv[2]));
const x = rt.exports;
const table = x.__indirect_function_table;
let failed = 0;
const check = (ok, msg) => { console.log(`${ok ? "PASS" : "FAIL"} ${msg}`); if (!ok) failed++; };

const idx = x.fnptr_add1();
check(typeof idx === "bigint" && idx > 0n && idx < BigInt(table.length),
      `c_fn_ptr(add1) = ${idx}: a function-table index (table length ${table.length})`);
check(rt.fn(idx)(41) === 42, "rt.fn(c_fn_ptr(add1))(41) === 42 (host-side call)");
let viaDrain = null;
rt.defer(() => { viaDrain = rt.call_fn(idx, 41); });
rt.drain();
check(viaDrain === 42, "rt.call_fn(c_fn_ptr(add1), 41) === 42 inside rt.drain()");
check(x.call_int_cb(idx, 41) === 42, "C call_indirect through c_fn_ptr(add1) returns 42");

const cb = x.fnptr_view_callback();
check(cb > 0n && cb < BigInt(table.length), `view callback = ${cb}: a table index`);
check(rt.fn(cb).length === 5,
      `view callback takes 5 wasm params (status, ptr, len, ud1, ud2), got ${rt.fn(cb).length}`);
let err = null;
try { rt.fn(cb)(7, 0n, 0n, x.slot_addr(), 0n); } catch (e) { err = e; }
check(err === null && x.slot_status() === 7, `callback through the table writes status 7 (got ${err ?? x.slot_status()})`);
process.exitCode = failed ? 1 : 0;
