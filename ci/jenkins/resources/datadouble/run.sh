#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Run one datadouble command for the `datadouble` step (vars/datadouble.groovy).
# The image runs as the engine's user (decision 86) on the host's network,
# with the agent's Docker daemon for the engines' containers and the
# workspace and the spec's host paths at the same paths, since engine
# containers mount them by those paths. DATADOUBLE_DOCKER and
# DATADOUBLE_DOCKER_SOCKET name another Docker command and the daemon's
# socket (decision 126); the socket is mounted at /var/run/docker.sock inside.
# The command's outputs go to .datadouble-outputs in the workspace, for the
# step to return.
set -euo pipefail

mapfile -t arguments < <(printf '%s\n' "$DATADOUBLE_ARGUMENTS" | sed '/^[[:space:]]*$/d')
bridge=$(mktemp -d "$WORKSPACE/.datadouble-ci.XXXXXX")
trap 'rm -rf "$bridge"' EXIT
chmod 0777 "$bridge"
rm -f "$WORKSPACE/.datadouble-outputs"

docker=${DATADOUBLE_DOCKER:-docker}
# The daemon's socket: DATADOUBLE_DOCKER_SOCKET, else a unix:// DOCKER_HOST, else
# Docker's default. A socket other than the default is also the daemon the
# image is started on (DOCKER_HOST, and podman's CONTAINER_HOST), so the image
# and the engines' containers share one daemon and one set of paths.
socket=${DATADOUBLE_DOCKER_SOCKET:-}
if [ -z "$socket" ] && [[ "${DOCKER_HOST:-}" == unix://* ]]; then socket=${DOCKER_HOST#unix://}; fi
if [ -n "$socket" ]; then export DOCKER_HOST="unix://$socket" CONTAINER_HOST="unix://$socket"; fi
socket=${socket:-/var/run/docker.sock}
if [ ! -S "$socket" ]; then
    echo "error: no Docker socket at $socket; set DATADOUBLE_DOCKER_SOCKET to the daemon's socket" >&2
    exit 1
fi
options=(
    --rm --network host
    --user "$DATADOUBLE_USER" --group-add "$(stat -L -c %g "$socket")"
    --volume "$socket:/var/run/docker.sock"
    --env DOCKER_HOST=unix:///var/run/docker.sock --env CONTAINER_HOST=unix:///var/run/docker.sock
    --volume "$WORKSPACE:$WORKSPACE" --workdir "$WORKSPACE"
    --env HOME=/tmp
    --env DATADOUBLE_BOUNDARY --env RUNNER_ENVIRONMENT --env CI_RUNNER_TAGS --env BUILD_URL
)
bridged=(--ci jenkins --ci-directory "$bridge")
if [ -n "${DATADOUBLE_CI_JOB:-}" ]; then bridged+=(--ci-job "$DATADOUBLE_CI_JOB"); fi
# Docker would create a missing path as root, which the engine's user cannot
# write, so a path the agent lacks fails here, saying what to create.
while IFS= read -r path; do
    if [ -z "$path" ]; then continue; fi
    if [ ! -e "$path" ]; then
        echo "error: $path does not exist on this agent; create it, owned by $DATADOUBLE_USER" >&2
        exit 1
    fi
    options+=(--volume "$path:$path")
done <<< "${DATADOUBLE_MOUNTS:-}"
while IFS= read -r name; do
    # Inside, the daemon is always the socket mounted at /var/run/docker.sock.
    case "$name" in ""|DOCKER_HOST|CONTAINER_HOST) continue ;; esac
    options+=(--env "$name")
done <<< "${DATADOUBLE_ENVIRONMENT:-}"
# A directory for files the step archives, such as the lock job's new lock:
# emptied of an earlier build's files, and writable by the engine's user.
if [ -n "${DATADOUBLE_ARTIFACTS:-}" ]; then
    rm -rf "${WORKSPACE:?}/$DATADOUBLE_ARTIFACTS"
    mkdir -p "$WORKSPACE/$DATADOUBLE_ARTIFACTS"
    chmod 0777 "$WORKSPACE/$DATADOUBLE_ARTIFACTS"
fi

status=0
"$docker" run "${options[@]}" "$DATADOUBLE_IMAGE" "${bridged[@]}" "${arguments[@]}" || status=$?
if [ -f "$bridge/outputs" ]; then cp "$bridge/outputs" "$WORKSPACE/.datadouble-outputs"; fi
exit "$status"
