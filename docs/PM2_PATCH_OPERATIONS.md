# PM2 fork spawn patch: maintenance and upgrade

This runbook records the `prohosting-1` fix deployed on 2026-11-10. It is
version-specific: **do not apply the 7.0.4 patch or copy its complete source
files onto another PM2 version** without a fresh review and tests.

## Background and current deployment

JP was blocked by a PM2 entry reporting `online` without a PID. The webhook
trusted the status and kept waiting. PM2's asynchronous fork spawn failure is
a demonstrated way to produce such a state, but missing historical logs mean
it is **not proven** to have caused the original incident.

The server uses global PM2 **7.0.4**, Node **24.21.0**, and the
`pm2-root.service` systemd unit. The updater patch changes
`lib/God.js`, `lib/God/ForkMode.js`, and `lib/God/ActionMethods.js`. The
separate webhook liveness and no-items fixes are deployed in
`/root/sekai-webhook/lib/`; do not overwrite its persisted retry state during
PM2 maintenance. The webhook intentionally errs on the side of keeping a lock
when process liveness is unknown; its describe/restart check is not atomic.

The versioned source diff is [`PM2_7.0.4_ASYNC_SPAWN.patch`](PM2_7.0.4_ASYNC_SPAWN.patch)
(SHA-256 `be8ef3de5846e6322ee67a1da83403e10f335da51c42f9eddd6f411481314032`).
Its upstream base is `Unitech/pm2` tag `v7.0.4`, commit
`cd6b1b4c592117212d7349d6932288613f336c15`. It has not been submitted upstream.
The accompanying [regression test patch](PM2_7.0.4_REGRESSION_TESTS.patch)
(SHA-256 `c8ff4036f9a43de2f8b151b1e16414412c0255e9237fd10cde4d7b1699b49433`)
preserves the five new test files and their npm script entries; both patches
apply to the 7.0.4 base in a separate development checkout. They do not apply
to a different release without review. The former machine-local development
worktree `.slim/worktrees/pm2-async-spawn` was removed after its three source
files, package metadata and five test files were reproduced byte-for-byte
from these patches. Recreate a separate checkout when needed; the ignored
dependency clone itself is read-only.
The read-only dependency clone under `.slim/clonedeps/repos/Unitech__pm2/`
is for inspection only.

| Installed PM2 file | Patched SHA-256 |
| --- | --- |
| `lib/God.js` | `69cab21923d24b72d4257f466050178fca45bb6d023ceaf13496ed89f40ecd4b` |
| `lib/God/ForkMode.js` | `a934e5ef297279dfbe5ac41cb713a1cb89370ad26796e5b558dae39652f1c9c9` |
| `lib/God/ActionMethods.js` | `71a692769758e99e7f70117ad03ef69a35ebb59ea71103ab3f06d3cfec437e7c` |

The original 7.0.4 files and the pre-deployment dump backups are in the
restricted server directory `/root/pm2-systemd-patch-n3WyKD/`. Treat dumps and
webhook state backups as secrets: they can contain environment values and
operational data. Do not print raw `pm2 jlist`, `pm2 describe`, `pm2 env`,
dumps, or configuration values into logs or tickets.

## Upstream check on 2026-11-14

The official [v7.1.2 release](https://github.com/Unitech/pm2/releases/tag/v7.1.2)
and the npm `latest` tag were newer than the deployed 7.0.4. v7.1.2 describes
a fix for `pm2 update` interacting with `ExecStop=pm2 kill` on systemd >=232.
The [v7.1.0 release](https://github.com/Unitech/pm2/releases/tag/v7.1.0)
fixes invalid PIDs contaminating `pidusage` monitoring. Neither release note
establishes that a fork spawn failure can no longer be reported online.

Static review of [v7.1.2 ForkMode](https://github.com/Unitech/pm2/blob/v7.1.2/lib/God/ForkMode.js)
and [God.executeApp](https://github.com/Unitech/pm2/blob/v7.1.2/lib/God.js)
found that the fork callback still precedes the child's `spawn` event and the
online transition still lacks a spawn-success/PID check. This is a static
observation, not a completed reproduction against 7.1.2. v7.1.2 also changes
`filter_env` behavior; assess compatibility before upgrading. Recheck current
releases and run the isolated failure test before any upgrade decision.

## Why `pm2 update` is unsafe on this host

Do not use `pm2 update` to load the patch. On 2026-11-10 it stopped the old
daemon and started a new one, after which systemd ran the unit's
`ExecStop=pm2 kill` and stopped the new daemon too. Webhook and log rotation
were unavailable until recovery. A manually started daemon can appear to be
adopted by systemd while remaining outside its cgroup. The safe procedure is a
clean stop followed by a **fresh start through systemd**; verify cgroup
membership rather than trusting `active` alone.

## Before a PM2 upgrade

1. Compare the proposed official release and source against the deployed
   patch. Reproduce the no-PID asynchronous `spawn` failure in an isolated
   PM2 home. If upstream has fixed it, verify its semantics; do not reapply
   this patch just because the version changed.
2. If still needed, port only applicable changes onto the new source. Review
   fork/cluster, `wait_ready`, rapid exit, stop/start races, generation and PID
   file handling, and bounded auto-restarts. The original PM2 failure was not
   conclusively diagnosed, so passing this reproduction does not prove its
   historical cause.
3. Run the new regression tests and the official PM2 unit suite in a disposable
   Linux environment with its own `HOME`, `PM2_HOME` and RPC sockets. The
   regression test patch provides `npm run test:fork-async-spawn` and
   `npm run test:pm2-update-recovery`.
   The latter's fake services do not validate the real systemd unit or its
   module. Never point tests at a production daemon.
4. Build a versioned, reviewable patch/package and record both baseline and
   patched file hashes. Reinstalling or upgrading the global npm package can
   silently remove this patch. Do not assume the webhook defense is a
   replacement for reviewing PM2.

## Production deployment checklist

Use a maintenance window and authorization. A systemd stop interrupts webhook
and log rotation; external webhook delivery/retry behavior during that interval
has not been verified. Do not replay deliveries or historical exhausted jobs
without reviewing them. Recheck all conditions immediately before stopping:

- Confirm the actual PM2 version, paths, systemd unit and loaded daemon;
  check the intended production code and staged artifact hashes.
- Check PM2 app status **and actual processes**, updater locks, webhook retry
  queue and timers. Do not stop a live updater or restart into an ambiguous
  process state. Verify external triggers, watch and `cron_restart` settings.
- Back up the exact currently installed PM2 files, both PM2 dump files,
  necessary unit/module configuration, and webhook retry state in a root-only
  directory. Do not restore a stale retry-state backup over new deliveries.
  Verify dump and fallback contain the intended stopped/online states before
  using them; do not log secrets. Do not use `pm2 save` on an unverified state.
- Stop `pm2-root.service` with systemd. Confirm the stop job is finished, the
  daemon and its children have actually exited, and no active RPC socket or
  PID file remains. Never use broad `pkill`/`killall` or delete live sockets.
- Install the complete reviewed patch set while the daemon is stopped,
  preserving owner/mode; verify the new hashes. Start via
  `systemctl start pm2-root.service`, not `pm2 update` or `reload all`.
- Verify service `active/running`, `MainPID` matches PM2's PID file, and the
  daemon, webhook and logrotate belong to `/system.slice/pm2-root.service`.
  Check webhook's side-effect-free `GET /api/github/webhooks` responds 404,
  logs show no startup or state-load error, and each updater has its expected
  state with no unexpected process. Inspect retry-state counts without exposing
  job contents. Observe a real subsequent update separately.

If startup or validation fails, stop the unit, verify all daemon processes
have exited, restore the exact pre-upgrade files with original permissions,
then start the verified original version through systemd and repeat health and
state checks. Restore a dump only after checking its content; do not overwrite
live retry state with a stale backup. Stop and investigate if any state or
process ownership is uncertain.
