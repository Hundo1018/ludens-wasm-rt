/* A library with host imports, for host.mjs composition / re-entry tests. */
#define IMPORT(n) __attribute__((import_module("env"), import_name(n)))
#define EXPORT(n) __attribute__((export_name(n)))

IMPORT("echo_cb") void echo_cb(int kind);
IMPORT("echo_drain") void echo_drain(void);

static int cell;

EXPORT("echo_run") void echo_run(int kind) { echo_cb(kind); }
EXPORT("echo_drain_run") void echo_drain_run(void) { echo_drain(); }
EXPORT("echo_cell_addr") int *echo_cell_addr(void) { return &cell; }
EXPORT("echo_cell") int echo_get(void) { return cell; }
