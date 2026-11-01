/* Wasm-side helpers for fnptr_probe.mjs (from wgpu-mojo tests/wasm/fnptr_caller.c). */
#include <stdint.h>

__attribute__((export_name("call_int_cb")))
int call_int_cb(int (*cb)(int), int x) { return cb(x); }

/* Where a callback's userdata1 points: a result slot in wasm memory. */
static uint32_t slot[4];

__attribute__((export_name("slot_addr")))
uint32_t *slot_addr(void) { return slot; }

__attribute__((export_name("slot_status")))
uint32_t slot_status(void) { return slot[0]; }
