/* The VM tab (#/vm): the daemon's VM panel (vm.html, vm.js; daemon/vm_api.py)
   inside the dashboard. The panel is the one implementation of the VM list,
   lifecycle, grants, registration and live screen, so the tab embeds it
   instead of copying it: opening the tab creates the frame, leaving the tab
   removes it, which also stops the panel's screen requests and its takeover
   heartbeat. The panel authenticates with the dashboard's own cookie. */
globalThis.VmPage = (() => {
  const view = () => document.getElementById("vm-view");
  const base = () => location.pathname.replace(/[^/]*$/, "");
  const src = () => `${base()}vm?embed=1`;

  function open() {
    const root = view();
    if (!root) return;
    let frame = root.querySelector("iframe");
    if (frame) return;
    frame = document.createElement("iframe");
    frame.id = "vm-frame";
    frame.title = "VM";
    frame.src = src();
    root.replaceChildren(frame);
  }

  function stop() {
    const root = view();
    if (root) root.replaceChildren();
  }

  return { open, stop, src };
})();
