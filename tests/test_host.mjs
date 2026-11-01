// host.mjs tests. Usage: node test_host.mjs <hello.wasm> <echo.wasm>
import { readFileSync } from "node:fs";
import { instantiate, mergeImports } from "../ludens_wasm_rt/host.mjs";
import * as echo from "./fixtures/echo/echo_host.mjs";

const [helloPath, echoPath] = process.argv.slice(2);
let failed = 0;
const check = (ok, msg) => { console.log(`${ok ? "PASS" : "FAIL"} ${msg}`); if (!ok) failed++; };
const throws = (fn, re) => { try { fn(); } catch (e) { return re.test(e.message) ? e.message : `wrong error: ${e.message}`; } return null; };

// instantiate + strings + table
const hello = await instantiate(readFileSync(helloPath));
check(hello.call("hello_kernel", 100) === 329520, "instantiate and call an export");
const [ptr, len] = hello.str_in("héllo wasm");
check(typeof ptr === "bigint" && len === 11n, `str_in returns BigInt [ptr, len] (${ptr}, ${len})`);
check(hello.str_out(ptr, len) === "héllo wasm", "str_out round-trips UTF-8");
const before = hello.mem();
hello.free(ptr);
check(hello.mem() instanceof ArrayBuffer && before.byteLength > 0, "rt.mem() gives the current ArrayBuffer");

// host_write / host_now_ns routed to the supplied host
let wrote = "";
const w = await instantiate(readFileSync(helloPath), { host: { write: (fd, t) => { wrote += t; }, now: () => 5n } });
check(w.instance && wrote === "", "custom host accepted");

// import composition: libs merge; a duplicate name is an error
check(throws(() => mergeImports({ env: { a: 1 } }, { env: { a: 2 } }), /provided twice/) !== null, "duplicate import name is refused");
const e = await instantiate(readFileSync(echoPath), { libs: [echo] });

// re-entry from inside a host import
e.call("echo_run", 1);
check(/re-entry: echo_run called from inside a host import/.test(e.reentry), `re-entry inside an import is blocked: ${e.reentry}`);

// defer / drain: applied only when Mojo enters a host import
const cellAddr = e.exports.echo_cell_addr();
e.defer(() => { new Int32Array(e.mem(), Number(cellAddr), 1)[0] = 77; });
check(e.exports.echo_cell() === 0, "deferred work has not run yet");
e.call("echo_drain_run");
check(e.drained === 1 && e.exports.echo_cell() === 77, "rt.drain() inside an import applies the deferred write");

// re-entry from a Promise callback: caught and named
let msg = null;
await e.when(Promise.resolve(1), function onDeviceReady() {
  try { e.call("echo_run", 2); } catch (err) { msg = err.message; }
});
check(msg && /re-entry: echo_run called from the Promise callback 'onDeviceReady'/.test(msg), `Promise re-entry is blocked and named: ${msg}`);
// after the callback the guard is lifted, and the deferred route is the sanctioned path
await e.when(Promise.resolve(1), () => e.defer(() => { new Int32Array(e.mem(), Number(cellAddr), 1)[0] = 5; }), "queue");
e.call("echo_drain_run");
check(e.exports.echo_cell() === 5, "defer from a Promise callback, drained at the next import");
const ok = e.call("echo_cell");
check(ok === 5, "calls work again outside the callback");
process.exitCode = failed ? 1 : 0;
