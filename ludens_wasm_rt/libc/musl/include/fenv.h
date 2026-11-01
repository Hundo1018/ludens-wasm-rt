/* wasm has no floating-point environment: round-to-nearest, no exception flags.
 * FE_INEXACT / FE_UNDERFLOW stay undefined so musl's flag-fixup blocks compile out. */
#ifndef _FENV_H
#define _FENV_H
#define FE_TONEAREST 0
static inline int fegetround(void) { return FE_TONEAREST; }
#endif
