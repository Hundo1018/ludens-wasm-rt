export function createImports(rt) {
  return {
    env: {
      // Tries to call back into the module from inside an import: must be refused.
      echo_cb: (kind) => {
        try { rt.call("echo_run", 0); rt.reentry = "allowed"; } catch (e) { rt.reentry = e.message; }
      },
      // Applies queued async results while Mojo is inside this import.
      echo_drain: () => { rt.drained = rt.drain(); },
    },
  };
}
