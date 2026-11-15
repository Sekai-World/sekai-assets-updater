const apps = [
  {
    name: "webhook-fixture",
    script: "/opt/handover-test/webhook.js",
    autorestart: true,
  },
  {
    name: "worker-fixture",
    script: "/opt/handover-test/worker.js",
    autorestart: true,
  },
  {
    name: "mock-logrotate",
    script: "/opt/handover-test/mock-logrotate.js",
    autorestart: true,
  },
  ...Array.from({ length: 5 }, (_, index) => ({
    name: `updater-fixture-${index + 1}`,
    script: "/opt/handover-test/updater.js",
    autorestart: false,
    env: { UPDATER_NAME: `updater-fixture-${index + 1}` },
  })),
];

module.exports = { apps };
