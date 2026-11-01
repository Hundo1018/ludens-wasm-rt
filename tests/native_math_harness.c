/* Native side of the math parity test: wrt_sinf / wrt_cosf from libwasmrt_math.a, fma from the platform libm. */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
float wrt_sinf(float);
float wrt_cosf(float);

int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb");
    uint32_t n, pad;
    fread(&n, 4, 1, f); fread(&pad, 4, 1, f);
    float *x = malloc(4 * (size_t)n * 4); double *d = malloc(8 * (size_t)n * 3);
    fread(x, 4, (size_t)n * 4, f); fread(d, 8, (size_t)n * 3, f);
    fclose(f);
    FILE *o = fopen(argv[2], "wb");
    float *r = malloc(4 * (size_t)n);
    for (uint32_t i = 0; i < n; i++) r[i] = wrt_sinf(x[i]);
    fwrite(r, 4, n, o);
    for (uint32_t i = 0; i < n; i++) r[i] = wrt_cosf(x[i]);
    fwrite(r, 4, n, o);
    for (uint32_t i = 0; i < n; i++) r[i] = fmaf(x[n + i], x[2 * n + i], x[3 * n + i]);
    fwrite(r, 4, n, o);
    double *rd = malloc(8 * (size_t)n);
    for (uint32_t i = 0; i < n; i++) rd[i] = fma(d[i], d[n + i], d[2 * n + i]);
    fwrite(rd, 8, n, o);
    fclose(o);
    return 0;
}
