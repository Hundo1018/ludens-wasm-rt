// node side of the math parity test. Usage: node math_run.mjs math.wasm in.bin out.bin
import { readFileSync, writeFileSync } from "node:fs";
import { instantiate } from "../ludens_wasm_rt/host.mjs";

const [wasm, inPath, outPath] = process.argv.slice(2);
const data = readFileSync(inPath);
const n = data.readUInt32LE(0);
const rt = await instantiate(readFileSync(wasm));
const body = data.subarray(8);                      // sin/cos in | fmaf a b c | fma a b c
const at = rt.exports.mw_alloc(BigInt(body.length), 64n);
rt.put(at, body);
const f4 = 4n * BigInt(n), f8 = 8n * BigInt(n);
const outBytes = f4 * 2n + f4 + f8;
const out = rt.exports.mw_alloc(outBytes, 64n);
const sinIn = at, fa = at + f4, fb = fa + f4, fc = fb + f4, da = fc + f4, db = da + f8, dc = db + f8;
const N = BigInt(n);
rt.call("k_sin_buf", sinIn, out, N);
rt.call("k_cos_buf", sinIn, out + f4, N);
rt.call("k_fmaf_buf", fa, fb, fc, out + 2n * f4, N);
rt.call("k_fma_buf", da, db, dc, out + 3n * f4, N);
writeFileSync(outPath, rt.bytes(out, outBytes));
