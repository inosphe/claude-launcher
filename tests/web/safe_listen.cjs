/* Shared by the *_browser.cjs harnesses: listen on an ephemeral loopback port
   that Chromium will actually navigate to.

   `server.listen(0)` takes whatever port the OS hands out. On a machine whose
   TCP dynamic range starts low (this one: 1024-15000) that can be a port
   Chromium refuses to load, and page.goto then fails with
   net::ERR_UNSAFE_PORT (claunch-h59l2: 6566 and 6668 were seen). */
const UNSAFE_PORTS = new Set([
  // Chromium net/base/port_util.cc, kRestrictedPorts, the list behind
  // net::IsPortAllowedForScheme (fetched 2026-10-08):
  // https://chromium.googlesource.com/chromium/src/+/main/net/base/port_util.cc
  0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
  87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
  139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532, 540,
  548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049,
  3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
]);

function isUnsafePort(port) {
  return UNSAFE_PORTS.has(port);
}

/* Listens on 127.0.0.1 with an OS-chosen port, closes and listens again while
   the port is on Chromium's restricted list. Resolves with the usable port. */
async function safeListen(server, host = "127.0.0.1", maxAttempts = 100) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, host, () => {
        server.removeListener("error", reject);
        resolve();
      });
    });
    const port = server.address().port;
    if (!isUnsafePort(port)) return port;
    await new Promise((resolve) => server.close(resolve));
  }
  throw new Error(`safeListen: no Chromium-safe port in ${maxAttempts} attempts`);
}

module.exports = { UNSAFE_PORTS, isUnsafePort, safeListen };
