#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Run one datadouble command for the action in this directory. The image runs
# as the engine's user (decision 86) on the host's network, with the runner's
# Docker daemon for the engines' containers and the workspace and the spec's
# host paths at the same paths, since engine containers mount them by those
# paths. The command writes outputs (its own, and the ci-outputs announce
# adapter's) and a summary into a directory in the workspace, handed on here;
# annotations and masks arrive on its output.
set -euo pipefail

mapfile -t arguments < <(printf '%s\n' "$DATADOUBLE_ARGUMENTS" | sed '/^[[:space:]]*$/d')
bridge=$(mktemp -d "$GITHUB_WORKSPACE/.datadouble-ci.XXXXXX")
trap 'rm -rf "$bridge"' EXIT
chmod 0777 "$bridge"

socket=/var/run/docker.sock
options=(
    --rm --network host
    --user "$DATADOUBLE_USER" --group-add "$(stat -c %g "$socket")"
    --volume "$socket:$socket"
    --volume "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" --workdir "$GITHUB_WORKSPACE"
    --env HOME=/tmp
    --env "GITHUB_OUTPUT=$bridge/outputs" --env "GITHUB_STEP_SUMMARY=$bridge/summary.md"
    --env DATADOUBLE_BOUNDARY --env RUNNER_ENVIRONMENT
    --env ACTIONS_ID_TOKEN_REQUEST_URL --env ACTIONS_ID_TOKEN_REQUEST_TOKEN
)
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
    if [ -n "$name" ]; then options+=(--env "$name"); fi
done <<< "$DATADOUBLE_ENVIRONMENT"

status=0
docker run "${options[@]}" "$DATADOUBLE_IMAGE" \
    --ci github-actions --ci-directory "$bridge" "${arguments[@]}" || status=$?
if [ -f "$bridge/outputs" ]; then cat "$bridge/outputs" >> "$GITHUB_OUTPUT"; fi
if [ -f "$bridge/summary.md" ]; then cat "$bridge/summary.md" >> "$GITHUB_STEP_SUMMARY"; fi
exit "$status"
