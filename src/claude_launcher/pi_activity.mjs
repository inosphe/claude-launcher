// Keep activity visible even when the daemon parks Pi's screen rendering.
// OSC is invisible terminal metadata; the daemon consumes it from live PTY
// output. The heartbeat expires if this process stops responding.
export default function (pi) {
  if (!process.env.CLAUNCH_SESSION) return;
  let heartbeat;
  const emit = (state) => process.stdout.write(`\x1b]777;claunch;activity;${state}\x07`);
  const stop = () => {
    if (heartbeat === undefined) return;
    clearInterval(heartbeat);
    heartbeat = undefined;
    emit("idle");
  };
  pi.on("agent_start", (_event, ctx) => {
    if (!ctx.hasUI) return;
    clearInterval(heartbeat);
    emit("busy");
    heartbeat = setInterval(() => emit("busy"), 5000);
    heartbeat.unref?.();
  });
  pi.on("agent_end", stop);
  pi.on("session_shutdown", stop);
}
