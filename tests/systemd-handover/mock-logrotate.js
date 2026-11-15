const fs = require("node:fs");

const log = "/var/lib/handover/mock-rotating.log";
const previous = `${log}.1`;
const rotateAtBytes = 1024;
const timer = setInterval(() => {
  fs.appendFileSync(log, `${new Date().toISOString()} mock logrotate fixture\n`);
  if (fs.statSync(log).size >= rotateAtBytes) {
    fs.rmSync(previous, { force: true });
    fs.renameSync(log, previous);
  }
}, 50);

process.on("SIGTERM", () => {
  clearInterval(timer);
  process.exit(0);
});
