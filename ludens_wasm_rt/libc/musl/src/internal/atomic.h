/* Only what musl's math sources need (no threads in the wasm64 runtime). */
#ifndef _INTERNAL_ATOMIC_H
#define _INTERNAL_ATOMIC_H
#include <stdint.h>
static inline int a_clz_64(uint64_t x) { return __builtin_clzll(x); }
#endif
