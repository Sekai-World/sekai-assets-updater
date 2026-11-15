const fs = require("node:fs");
const path = require("node:path");

const name = process.env.UPDATER_NAME;
if (!name) throw new Error("UPDATER_NAME is required");

const counterPath = path.join("/var/lib/handover/counters", `${name}.count`);
const current = Number(fs.readFileSync(counterPath, "utf8").trim());
fs.writeFileSync(counterPath, `${current + 1}\n`);

// These represent one-shot updater jobs while remaining observable in PM2.
setInterval(() => {}, 1000);
