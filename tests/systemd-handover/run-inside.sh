#!/usr/bin/env bash
set -uo pipefail

readonly SERVICE=pm2-fixture.service
readonly HOME_DIR=/var/lib/handover
readonly PM2_HOME_DIR="$HOME_DIR/.pm2"
readonly FIXTURE_DIR=/opt/handover-test
readonly UPDATERS=(updater-fixture-1 updater-fixture-2 updater-fixture-3 updater-fixture-4 updater-fixture-5)
readonly LIVE_APPS=(webhook-fixture worker-fixture mock-logrotate)
passed=0
failed=0

check() {
  local label="$1"
  shift
  if "$@"; then
    printf 'PASS: %s\n' "$label"
    passed=$((passed + 1))
  else
    printf 'FAIL: %s\n' "$label"
    failed=$((failed + 1))
  fi
}

check_equal() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf 'PASS: %s (%s)\n' "$label" "$actual"
    passed=$((passed + 1))
  else
    printf 'FAIL: %s (expected %s, got %s)\n' "$label" "$expected" "${actual:-<empty>}"
    failed=$((failed + 1))
  fi
}

as_handover() {
  env HOME="$HOME_DIR" PM2_HOME="$PM2_HOME_DIR" \
    PATH=/usr/local/bin:/usr/bin:/bin "$@"
}

pm2_json() {
  as_handover pm2 jlist
}

wait_until() {
  local attempts="$1"
  shift
  for _ in $(seq 1 "$attempts"); do
    "$@" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

pm2_process_state_is() {
  local name="$1" wanted="$2"
  pm2_json | jq -e --arg name "$name" --arg wanted "$wanted" \
    '[.[] | select(.name == $name and .pm2_env.status == $wanted)] | length == 1' >/dev/null
}

all_expected_states() {
  local name
  for name in "${LIVE_APPS[@]}"; do
    pm2_process_state_is "$name" online || return 1
  done
  for name in "${UPDATERS[@]}"; do
    pm2_process_state_is "$name" stopped || return 1
  done
}

updater_counters_are_zero() {
  local name value
  for name in "${UPDATERS[@]}"; do
    value="$(cat "$HOME_DIR/counters/$name.count" 2>/dev/null || printf missing)"
    [[ "$value" == 0 ]] || return 1
  done
}

webhook_is_healthy() {
  curl --fail --silent http://127.0.0.1:18765/health \
    | jq -e '.status == "ok" and (.pid | type == "number")' >/dev/null
}

webhook_is_down() {
  ! webhook_is_healthy
}

worker_heartbeat_is_fresh() {
  local timestamp now
  timestamp="$(awk '{print $1}' "$HOME_DIR/worker-heartbeat" 2>/dev/null || true)"
  [[ "$timestamp" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s%3N)"
  (( now - timestamp < 3000 ))
}

pm2_pid() {
  cat "$PM2_HOME_DIR/pm2.pid" 2>/dev/null
}

process_in_cgroup() {
  local pid="$1" expected="$2" hierarchy controllers path
  [[ -n "$expected" && "$expected" != / ]] || return 1
  [[ -r "/proc/$pid/cgroup" ]] || return 1
  while IFS=: read -r hierarchy controllers path; do
    [[ "$path" == "$expected" || "$path" == "$expected/"* ]] && return 0
  done < "/proc/$pid/cgroup"
  return 1
}

all_live_apps_in_cgroup() {
  local expected="$1" name pid
  for name in "${LIVE_APPS[@]}"; do
    pid="$(pm2_json | jq -er --arg name "$name" '.[] | select(.name == $name) | .pid')" || return 1
    process_in_cgroup "$pid" "$expected" || return 1
  done
}

wait_for_unit_active() {
  [[ "$(systemctl show "$SERVICE" --property=ActiveState --value 2>/dev/null)" == active ]]
}

report_state() {
  printf '\n--- systemd unit state ---\n'
  systemctl show "$SERVICE" --property=ActiveState --property=SubState \
    --property=MainPID --property=ControlGroup --property=Restart \
    --property=KillMode --property=PIDFile --property=NRestarts || true
  printf '\n--- PM2 process state ---\n'
  pm2_json | jq -r '.[] | [.name, .pm2_env.status, (.pid | tostring)] | @tsv' 2>&1 || true
}

printf 'Systemd PID 1: %s\n' "$(cat /proc/1/comm)"
if [[ "$(cat /proc/1/comm)" != systemd ]]; then
  printf 'BLOCKED: systemd is not PID 1 inside the isolated container.\n' >&2
  exit 2
fi

install -d "$HOME_DIR/counters" "$PM2_HOME_DIR"
printf 'PM2 version: %s\n' "$(as_handover pm2 --version 2>&1 | tail -n 1)"
for name in "${UPDATERS[@]}"; do
  printf '0\n' > "$HOME_DIR/counters/$name.count"
done
systemctl daemon-reload

printf '\n--- manual PM2 fixture setup ---\n'
as_handover pm2 start "$FIXTURE_DIR/ecosystem.config.cjs"
for name in "${LIVE_APPS[@]}" "${UPDATERS[@]}"; do
  check "initial PM2 process started: $name" wait_until 20 pm2_process_state_is "$name" online
done
for name in "${UPDATERS[@]}"; do
  as_handover pm2 stop "$name" >/dev/null
done
sleep 1

# Exclude expected fixture registration boots; zero is the observation baseline.
for name in "${UPDATERS[@]}"; do
  printf '0\n' > "$HOME_DIR/counters/$name.count"
done
as_handover pm2 save --force

check 'manual PM2 state has three online apps and five stopped updaters' all_expected_states
check 'PM2 dump contains all eight isolated fixtures' \
  bash -c 'jq -e "length == 8" "$1" >/dev/null' _ "$PM2_HOME_DIR/dump.pm2"
check 'updater counters are zero before handover' updater_counters_are_zero
check 'mock logrotate fixture performed a rotation' test -s "$HOME_DIR/mock-rotating.log.1"
check 'webhook health is good before handover' webhook_is_healthy

manual_pid="$(pm2_pid || true)"
printf '\n--- manual daemon -> systemd handover ---\n'
if ! systemctl start "$SERVICE"; then
  systemctl status "$SERVICE" --no-pager || true
  journalctl -u "$SERVICE" --no-pager -n 40 || true
fi
check 'systemd reports the manual-handover unit active' wait_until 15 wait_for_unit_active
handover_pid="$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || true)"
handover_cgroup="$(systemctl show "$SERVICE" --property=ControlGroup --value 2>/dev/null || true)"
report_state
printf 'Pre-existing manual PM2 daemon cgroup: '
cat "/proc/$manual_pid/cgroup" 2>/dev/null || true
check_equal 'systemd MainPID matches the pre-existing manual daemon' "$handover_pid" "$manual_pid"
check 'manual daemon is in the unit cgroup after handover' \
  process_in_cgroup "$manual_pid" "$handover_cgroup"
check 'manual PM2 children are in the unit cgroup after handover' \
  all_live_apps_in_cgroup "$handover_cgroup"
check 'manual handover preserves PM2 states' all_expected_states
check 'manual handover does not start any updater' updater_counters_are_zero
check 'webhook health remains good after handover' webhook_is_healthy
check 'mock logrotate remains online after handover' pm2_process_state_is mock-logrotate online

printf '\n--- stop adopted daemon before fresh systemd start ---\n'
systemctl stop "$SERVICE" || true
as_handover pm2 kill >/dev/null 2>&1 || true
wait_until 15 test ! -e "$PM2_HOME_DIR/pm2.pid" || true

printf '\n--- fresh systemd-owned start ---\n'
systemctl start "$SERVICE" || true
check 'systemd-owned service reaches active state' wait_until 30 wait_for_unit_active
check 'PM2 restores expected online/stopped process states' wait_until 30 all_expected_states
check 'fresh start does not start any updater' updater_counters_are_zero
check 'webhook health is good after fresh start' webhook_is_healthy
check 'second service heartbeat is fresh' worker_heartbeat_is_fresh
check 'mock logrotate remains online after fresh start' pm2_process_state_is mock-logrotate online

systemd_pid="$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || true)"
systemd_cgroup="$(systemctl show "$SERVICE" --property=ControlGroup --value 2>/dev/null || true)"
check 'fresh systemd MainPID matches PM2 PIDFile' test "$systemd_pid" = "$(pm2_pid || true)"
check 'fresh PM2 daemon is in the unit cgroup' process_in_cgroup "$systemd_pid" "$systemd_cgroup"
check 'all live PM2 processes are in the unit cgroup' all_live_apps_in_cgroup "$systemd_cgroup"
check_equal 'Restart policy is on-failure' \
  "$(systemctl show "$SERVICE" --property=Restart --value)" on-failure
check_equal 'unit type is forking' \
  "$(systemctl show "$SERVICE" --property=Type --value)" forking
check_equal 'KillMode is control-group' \
  "$(systemctl show "$SERVICE" --property=KillMode --value)" control-group
check_equal 'PIDFile uses the isolated PM2 home' \
  "$(systemctl show "$SERVICE" --property=PIDFile --value)" "$PM2_HOME_DIR/pm2.pid"
report_state

printf '\n--- unexpected daemon failure and systemd recovery ---\n'
before_pid="$(systemctl show "$SERVICE" --property=MainPID --value)"
before_restarts="$(systemctl show "$SERVICE" --property=NRestarts --value)"
kill -KILL "$before_pid"
recovered=false
for _ in $(seq 1 40); do
  state="$(systemctl show "$SERVICE" --property=ActiveState --value 2>/dev/null || true)"
  current_pid="$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || true)"
  restarts="$(systemctl show "$SERVICE" --property=NRestarts --value 2>/dev/null || true)"
  if [[ "$state" == active && "$current_pid" =~ ^[1-9][0-9]*$ \
    && "$current_pid" != "$before_pid" && "$restarts" =~ ^[1-9][0-9]*$ \
    && "$restarts" -gt "$before_restarts" ]] \
    && all_expected_states && webhook_is_healthy; then
    recovered=true
    break
  fi
  sleep 1
done
check 'Restart=on-failure restarted PM2 after SIGKILL' test "$recovered" = true
check 'failure recovery did not start any updater' updater_counters_are_zero
check 'PM2 fixture states recover after daemon failure' all_expected_states
check 'webhook health recovers after daemon failure' webhook_is_healthy
report_state

printf '\n--- rollback to manual PM2 management ---\n'
systemctl stop "$SERVICE" || true
check 'systemd rollback stop leaves the unit inactive' \
  test "$(systemctl show "$SERVICE" --property=ActiveState --value)" = inactive
check 'ExecStop pm2 kill removes the PM2 PIDFile' \
  wait_until 15 test ! -e "$PM2_HOME_DIR/pm2.pid"
check 'webhook stops before manual rollback' wait_until 10 webhook_is_down
as_handover pm2 resurrect
check 'manual rollback restores expected PM2 states' wait_until 30 all_expected_states
check 'manual rollback keeps all updater counters at zero' updater_counters_are_zero
check 'webhook health is good after manual rollback' webhook_is_healthy
check 'mock logrotate is restored after manual rollback' pm2_process_state_is mock-logrotate online

as_handover pm2 kill >/dev/null 2>&1 || true
systemctl stop "$SERVICE" >/dev/null 2>&1 || true

printf '\n--- final results ---\n'
printf 'Checks passed: %s\nChecks failed: %s\n' "$passed" "$failed"
if (( failed > 0 )); then
  printf 'RESULT: FAIL (not all isolated handover criteria were met)\n'
  exit 1
fi
printf 'RESULT: PASS (synthetic isolated fixture only; not a production/deployment test)\n'
