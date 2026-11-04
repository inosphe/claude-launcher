/* The VM pane in a session's split view (🖥 vm): the VM page's session mode
   (vm.js, `vm?embed=1&session=<name>`) in a frame under the terminal. The
   page is the one implementation of grants, shared folders and the one-click
   new VM, and every call it makes is a user-only route, so the pane adds no
   route and a session cannot invoke any of it. Mounting creates the frame and
   unmounting removes it, which also stops the page's own timers. */
globalThis.VmSessionPane = (() => {
  const base = () => location.pathname.replace(/[^/]*$/, "");
  const src = (name) => `${base()}vm?embed=1&session=${encodeURIComponent(name)}`;

  function mount(body, name) {
    const frame = document.createElement("iframe");
    frame.className = "vm-session-frame";
    frame.title = `VM · ${name}`;
    frame.src = src(name);
    body.classList.add("vm-session-host");
    body.replaceChildren(frame);
  }

  function unmount() {}

  return { mount, unmount, src };
})();
