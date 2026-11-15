#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="pm2-systemd-handover-fixture:$$"
CONTAINER="pm2-systemd-handover-$$"

die() {
  printf 'BLOCKED: %s\n' "$*" >&2
  exit 2
}

command -v docker >/dev/null 2>&1 || die "Docker CLI is unavailable"
context="$(docker context show 2>/dev/null || true)"
[[ -n "$context" ]] || die "could not identify the Docker context"
daemon_os="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
[[ "$daemon_os" == linux ]] || die "Docker daemon is not Linux (context: $context)"

case "$context" in
  orbstack|desktop-linux|colima|rancher-desktop|podman-machine-default)
    ;;
  *)
    [[ "${SYSTEMD_HANDOVER_ISOLATED_VM_CONFIRMED:-}" == yes ]] || die \
      "Docker context '$context' is not a recognized desktop Linux VM; only run inside a disposable isolated VM, then set SYSTEMD_HANDOVER_ISOLATED_VM_CONFIRMED=yes"
    ;;
esac

cleanup() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

printf 'Docker context: %s (Linux daemon)\n' "$context"
printf 'Building isolated fixture image %s\n' "$IMAGE"
docker build --tag "$IMAGE" "$SCRIPT_DIR"

printf 'Starting disposable, network-isolated systemd container\n'
docker run --detach \
  --name "$CONTAINER" \
  --privileged \
  --cgroupns=private \
  --network none \
  --tmpfs /run \
  --tmpfs /run/lock \
  "$IMAGE" >/dev/null

ready=false
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" sh -c \
    'test "$(cat /proc/1/comm)" = systemd && systemctl list-units --no-legend >/dev/null 2>&1'; then
    ready=true
    break
  fi
  running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)"
  [[ "$running" == true ]] || break
  sleep 1
done

if [[ "$ready" != true ]]; then
  printf 'BLOCKED: full systemd did not become PID 1 in the isolated container.\n' >&2
  docker logs "$CONTAINER" >&2 || true
  docker exec "$CONTAINER" journalctl --no-pager -b 2>&1 >&2 || true
  exit 2
fi

docker exec "$CONTAINER" /usr/local/sbin/run-handover-test
