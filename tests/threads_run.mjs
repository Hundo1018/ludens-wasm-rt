// test_threads.py's runner: one shared memory64, one --threads module, N instances.
//   node tests/threads_run.mjs threads.wasm N_ITEMS
// Fills N_ITEMS values three times: on the main instance alone, on 1 Worker, on 2 Workers (each Worker is one more
// instance of the module on the same memory, with its own stack from mw_alloc), and prints one JSON line with the
// number of differing bit patterns against the single-instance run and the allocator's live bytes before / after.
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import { readFileSync } from 'node:fs';

const STACK = 1n << 20n;  // the module's own stack is 1 MiB (wasm-ld -z stack-size); each Worker gets the same
const env = (memory) => ({ env: { memory, host_write: () => {}, host_now_ns: () => 0n } });

// The descriptor key for a 64-bit memory is `address` in the current JS API; node 24 also accepted `index`.
function sharedMemory64() {
  for (const key of ['address', 'index']) {
    try { return new WebAssembly.Memory({ initial: 64n, maximum: 65536n, shared: true, [key]: 'i64' }); } catch {}
  }
  throw new Error('this node cannot create a shared memory64');
}

function runWorkers(module, memory, stacks, base, n) {
  const chunk = Math.ceil(n / stacks.length);
  return Promise.all(stacks.map((sp, w) => new Promise((resolve, reject) => {
    const worker = new Worker(new URL(import.meta.url), {
      workerData: { module, memory, sp, base, lo: Math.min(n, w * chunk), hi: Math.min(n, (w + 1) * chunk) } });
    worker.once('message', resolve);
    worker.once('error', reject);
  })));
}

if (isMainThread) {
  const [file, nArg] = process.argv.slice(2);
  const n = Number(nArg ?? 2000);
  const module = new WebAssembly.Module(readFileSync(file));
  const memory = sharedMemory64();
  const x = new WebAssembly.Instance(module, env(memory)).exports;
  const outs = [0, 1, 2].map(() => x.mw_alloc(BigInt(n * 8), 8n));
  const stacks = [0, 1].map(() => x.mw_alloc(STACK, 16n) + STACK);  // a stack grows down from the top
  const liveBefore = x.rt_live_bytes();
  x.threads_fill(outs[0], 0n, BigInt(n));
  const liveAfterSerial = x.rt_live_bytes();
  await runWorkers(module, memory, stacks.slice(0, 1), outs[1], n);
  await runWorkers(module, memory, stacks, outs[2], n);
  const view = (p) => new BigUint64Array(memory.buffer, Number(p), n);
  const serial = view(outs[0]);
  const differing = (p) => { const v = view(p); let d = 0; for (let i = 0; i < n; i++) d += v[i] !== serial[i]; return d; };
  let zeros = 0; for (let i = 0; i < n; i++) zeros += serial[i] === 0n;
  console.log(JSON.stringify({
    n, zeros, differing_1_worker: differing(outs[1]), differing_2_workers: differing(outs[2]),
    live_before: Number(liveBefore), live_after_serial: Number(liveAfterSerial), live_after: Number(x.rt_live_bytes()),
    peak: Number(x.rt_peak_bytes()),
  }));
} else {
  const { module, memory, sp, base, lo, hi } = workerData;
  const x = new WebAssembly.Instance(module, env(memory)).exports;
  x.__stack_pointer.value = sp;
  x.threads_fill(base, BigInt(lo), BigInt(hi));
  parentPort.postMessage(0);
}
