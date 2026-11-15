# Isolated PM2 → systemd handover test

This disposable integration fixture tests the PM2/systemd ownership question.
It does not read production configuration, PM2 state, or host files. Its image
contains PM2 7.0.4 and a private unit with this topology:

- `Type=forking` and a private `PIDFile`
- `ExecStart=pm2 resurrect` / `ExecStop=pm2 kill`
- `Restart=on-failure` and `KillMode=control-group`
- Two continuously running service fixtures, a mock logrotate process, and five
  stopped updater fixtures with startup counters reset before handover

The test starts PM2 manually, saves the fixture state, and asks systemd to adopt
that daemon. It checks MainPID, cgroup membership, PM2 states, webhook health,
mock rotation, and updater counters. It then checks a clean systemd-owned start,
forced daemon failure and recovery, and rollback to manual PM2 management.

## Run

Run from a disposable Linux VM or desktop container runtime backed by its own
Linux VM (OrbStack, Docker Desktop, Colima, etc.):

```sh
bash tests/systemd-handover/run.sh
```

The runner requires a Linux Docker daemon and a recognized desktop-VM context.
For a separately confirmed disposable Linux VM with another context, set
`SYSTEMD_HANDOVER_ISOLATED_VM_CONFIRMED=yes` explicitly before running. Never
run against a production Docker daemon or a host whose systemd/PM2 state is in
scope.

The runner builds only this directory, then starts a disposable privileged
container with a private cgroup namespace, no network, no host filesystem
bind-mounts, and no persistent volumes. All `systemctl` operations run inside
that container with systemd as PID 1. The container is force-removed on exit.
The PM2 fixture runs as root only inside this disposable container so the
system-service PIDFile has production-like ownership. It never runs
`systemctl` on the host and never uses a real PM2 home/config.

## Interpretation

- `BLOCKED` means full systemd could not be established in the isolated
  container; it is not evidence that handover passed or failed.
- `FAIL` means at least one asserted behavior was not established. Manual
  daemon adoption that reports an active unit while the daemon or its children
  remain outside the unit cgroup is a failed ownership handover, even if the
  webhook responds.
- `PASS` applies only to this synthetic fixture and does not authorize or
  perform a production migration.
