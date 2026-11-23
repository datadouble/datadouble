#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Run one datadouble command for the `datadouble` step on a Kubernetes agent,
# in the Datadouble container of the job's pod (vars/datadouble.groovy). That
# container runs as the engine's user (decision 86) with the side's work claim
# mounted where the spec's kubernetes runtime mounts it in the engine pods, so
# this runs datadouble directly. The production boundary mark comes only from
# the namespace's datadouble-boundary ConfigMap, which the pod mounts as files:
# Jenkins lays its own environment (global variables, withEnv) over the
# container's, so no environment variable is trusted for it. The command's
# outputs go to .datadouble-outputs in the workspace, for the step to return;
# the agent container's umask leaves the workspace writable for this user.
# Naming the job lets the command post the spec's notify webhooks, linking
# BUILD_URL, which Jenkins sets in the container's environment.
set -euo pipefail

mapfile -t arguments < <(printf '%s\n' "$DATADOUBLE_ARGUMENTS" | sed '/^[[:space:]]*$/d')
bridge=$(mktemp -d "$WORKSPACE/.datadouble-ci.XXXXXX")
trap 'rm -rf "$bridge"' EXIT
rm -f "$WORKSPACE/.datadouble-outputs"

export HOME=/tmp
unset DATADOUBLE_BOUNDARY
mark=/etc/datadouble-boundary/DATADOUBLE_BOUNDARY
if [ -f "$mark" ]; then
    DATADOUBLE_BOUNDARY=$(cat "$mark")
    export DATADOUBLE_BOUNDARY
fi

bridged=(--ci jenkins --ci-directory "$bridge")
if [ -n "${DATADOUBLE_CI_JOB:-}" ]; then bridged+=(--ci-job "$DATADOUBLE_CI_JOB"); fi
# A directory for files the step archives, such as the lock job's new lock,
# emptied of an earlier build's files.
if [ -n "${DATADOUBLE_ARTIFACTS:-}" ]; then
    rm -rf "${WORKSPACE:?}/$DATADOUBLE_ARTIFACTS"
    mkdir -p "$WORKSPACE/$DATADOUBLE_ARTIFACTS"
fi

status=0
datadouble "${bridged[@]}" "${arguments[@]}" || status=$?
if [ -f "$bridge/outputs" ]; then cp "$bridge/outputs" "$WORKSPACE/.datadouble-outputs"; fi
exit "$status"
