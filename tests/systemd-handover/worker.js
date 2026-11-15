const fs = require("node:fs");

const heartbeat = "/var/lib/handover/worker-heartbeat";
const tick = () => fs.writeFileSync(heartbeat, `${Date.now()} ${process.pid}\n`);

tick();
const timer = setInterval(tick, 250);
process.on("SIGTERM", () => {
  clearInterval(timer);
  process.exit(0);
});
