#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Run one datadouble command for the action in this directory. The image runs
# as the engine's user (decision 86) on the host's network, with the runner's
# Docker daemon for the engines' containers and the workspace and the spec's
# host paths at the same paths, since engine containers mount them by those
# paths. DATADOUBLE_DOCKER and DATADOUBLE_DOCKER_SOCKET name another Docker
# command and the daemon's socket (decision 126); the socket is mounted at
# /var/run/docker.sock inside. The command writes outputs (its own, and the
# ci-outputs announce adapter's) and a summary into a directory in the
# workspace, handed on here; annotations and masks arrive on its output.
# Naming the job lets the command post the spec's notify webhooks, with the
# run's page from GitHub's variables.
set -euo pipefail

mapfile -t arguments < <(printf '%s\n' "$DATADOUBLE_ARGUMENTS" | sed '/^[[:space:]]*$/d')
bridge=$(mktemp -d "$GITHUB_WORKSPACE/.datadouble-ci.XXXXXX")
trap 'rm -rf "$bridge"' EXIT
chmod 0777 "$bridge"

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
    echo "::error title=Datadouble::no Docker socket at $socket; set DATADOUBLE_DOCKER_SOCKET to the daemon's socket"
    exit 1
fi
options=(
    --rm --network host
    --user "$DATADOUBLE_USER" --group-add "$(stat -L -c %g "$socket")"
    --volume "$socket:/var/run/docker.sock"
    --env DOCKER_HOST=unix:///var/run/docker.sock --env CONTAINER_HOST=unix:///var/run/docker.sock
    --volume "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" --workdir "$GITHUB_WORKSPACE"
    --env HOME=/tmp
    --env "GITHUB_OUTPUT=$bridge/outputs" --env "GITHUB_STEP_SUMMARY=$bridge/summary.md"
    --env DATADOUBLE_BOUNDARY --env RUNNER_ENVIRONMENT
    --env ACTIONS_ID_TOKEN_REQUEST_URL --env ACTIONS_ID_TOKEN_REQUEST_TOKEN
    --env GITHUB_SERVER_URL --env GITHUB_REPOSITORY --env GITHUB_RUN_ID
)
bridged=(--ci github-actions --ci-directory "$bridge")
if [ -n "${DATADOUBLE_CI_JOB:-}" ]; then bridged+=(--ci-job "$DATADOUBLE_CI_JOB"); fi
# Docker would create a missing path as root, which the engine's user cannot
# write, so a path the runner lacks fails here, saying what to create.
while IFS= read -r path; do
    if [ -z "$path" ]; then continue; fi
    if [ ! -e "$path" ]; then
        echo "::error title=Datadouble::$path does not exist on this runner; create it, owned by $DATADOUBLE_USER"
        exit 1
    fi
    options+=(--volume "$path:$path")
done <<< "$DATADOUBLE_MOUNTS"
while IFS= read -r name; do
    # Inside, the daemon is always the socket mounted at /var/run/docker.sock.
    case "$name" in ""|DOCKER_HOST|CONTAINER_HOST) continue ;; esac
    options+=(--env "$name")
done <<< "$DATADOUBLE_ENVIRONMENT"
# A directory for files the workflow publishes, such as the lock job's new
# lock: emptied of an earlier run's files, and writable by the engine's user.
if [ -n "${DATADOUBLE_ARTIFACTS:-}" ]; then
    rm -rf "${GITHUB_WORKSPACE:?}/$DATADOUBLE_ARTIFACTS"
    mkdir -p "$GITHUB_WORKSPACE/$DATADOUBLE_ARTIFACTS"
    chmod 0777 "$GITHUB_WORKSPACE/$DATADOUBLE_ARTIFACTS"
fi

status=0
"$docker" run "${options[@]}" "$DATADOUBLE_IMAGE" "${bridged[@]}" "${arguments[@]}" || status=$?
if [ -f "$bridge/outputs" ]; then cat "$bridge/outputs" >> "$GITHUB_OUTPUT"; fi
if [ -f "$bridge/summary.md" ]; then cat "$bridge/summary.md" >> "$GITHUB_STEP_SUMMARY"; fi
exit "$status"
