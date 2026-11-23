#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Run one datadouble command for the `datadouble` step (vars/datadouble.groovy).
# The image runs as the engine's user (decision 86) on the host's network,
# with the agent's Docker daemon for the engines' containers and the
# workspace and the spec's host paths at the same paths, since engine
# containers mount them by those paths. The command's outputs go to
# .datadouble-outputs in the workspace, for the step to return.
set -euo pipefail

mapfile -t arguments < <(printf '%s\n' "$DATADOUBLE_ARGUMENTS" | sed '/^[[:space:]]*$/d')
bridge=$(mktemp -d "$WORKSPACE/.datadouble-ci.XXXXXX")
trap 'rm -rf "$bridge"' EXIT
chmod 0777 "$bridge"
rm -f "$WORKSPACE/.datadouble-outputs"

socket=/var/run/docker.sock
options=(
    --rm --network host
    --user "$DATADOUBLE_USER" --group-add "$(stat -c %g "$socket")"
    --volume "$socket:$socket"
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
    if [ -n "$name" ]; then options+=(--env "$name"); fi
done <<< "${DATADOUBLE_ENVIRONMENT:-}"
# A directory for files the step archives, such as the lock job's new lock:
# emptied of an earlier build's files, and writable by the engine's user.
if [ -n "${DATADOUBLE_ARTIFACTS:-}" ]; then
    rm -rf "${WORKSPACE:?}/$DATADOUBLE_ARTIFACTS"
    mkdir -p "$WORKSPACE/$DATADOUBLE_ARTIFACTS"
    chmod 0777 "$WORKSPACE/$DATADOUBLE_ARTIFACTS"
fi

status=0
docker run "${options[@]}" "$DATADOUBLE_IMAGE" "${bridged[@]}" "${arguments[@]}" || status=$?
if [ -f "$bridge/outputs" ]; then cp "$bridge/outputs" "$WORKSPACE/.datadouble-outputs"; fi
exit "$status"
