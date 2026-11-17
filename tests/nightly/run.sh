#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The nightly hosted run's steps (.github/workflows/nightly.yml), on the
# GitHub-hosted machine that also hosts the night's self-hosted runner:
#
#   run.sh source      start the shop database and the directories the spec names
#   run.sh pipeline    run the generated workflows in order, checking each one
#   run.sh refusal     check this hosted machine is refused even when marked inside
#
# The generated workflows (scripts/nightly.py) run on the self-hosted runner;
# `gh` starts them with the job's own token and follows their runs.
set -euo pipefail

network=datadouble-nightly
base=/var/tmp/datadouble-nightly
spec=tests/nightly/nightly.yml

source_database() {
    local password
    password=$(python3 -c 'import os, urllib.parse
print(urllib.parse.urlsplit(os.environ["DATADOUBLE_NIGHTLY_DSN"]).password)')
    docker network create --subnet 172.30.0.0/24 "$network" > /dev/null
    POSTGRES_PASSWORD=$password docker run --detach --name datadouble-nightly-source \
        --network "$network" --ip 172.30.0.10 --env POSTGRES_PASSWORD postgres:18.4-trixie > /dev/null
    docker exec datadouble-nightly-source timeout 60 sh -c \
        'until pg_isready --quiet --host 127.0.0.1 --username postgres; do sleep 1; done'
    docker exec --interactive datadouble-nightly-source \
        psql --username postgres --quiet --set ON_ERROR_STOP=1 < tests/nightly/shop.sql
    sudo install -d -o 999 -g 999 -m 0755 "$base" "$base/store" "$base/export" \
        "$base/work" "$base/staging" "$base/copies"
}

# The newest run of a workflow started after a time, once there is one.
newest_run() {
    local workflow=$1 after=$2 run
    for _ in $(seq 60); do
        run=$(gh run list --workflow "$workflow" --limit 5 --json databaseId,createdAt \
            --jq "[.[] | select(.createdAt > \"$after\")][0].databaseId // empty")
        if [ -n "$run" ]; then echo "$run"; return; fi
        sleep 10
    done
    echo "no run of $workflow started after $after" >&2
    return 1
}

# Start a workflow, or wait for one another starts, and require it to succeed.
finish() {
    local workflow=$1 started=$2 run
    run=$(newest_run "$workflow" "$started")
    echo "following $workflow, run $run"
    gh run watch "$run" --exit-status --interval 15 > /dev/null
}

start() {
    local workflow=$1 started
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    gh workflow run "$workflow" --ref "$GITHUB_REF_NAME"
    finish "$workflow" "$started"
}

pipeline() {
    local started
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    start datadouble-nightly-build.yml
    finish datadouble-nightly-promote.yml "$started"     # follows build
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    start datadouble-nightly-fetch.yml
    finish datadouble-nightly-provision.yml "$started"   # follows fetch
    start datadouble-nightly-prune-production.yml
    start datadouble-nightly-prune-non-production.yml
    local copies
    copies=$(docker run --rm --user 999:999 --volume "$base:$base" \
        --volume "$PWD:$PWD" --workdir "$PWD" datadouble:nightly \
        --json copies --spec "$spec" | jq '.result.copies | length')
    if [ "$copies" != 1 ]; then
        echo "the pool holds $copies copies, not 1" >&2
        return 1
    fi
}

refusal() {
    local out status=0
    out=$(docker run --rm --network host --user 999:999 \
        --env DATADOUBLE_BOUNDARY=inside --env RUNNER_ENVIRONMENT --env DATADOUBLE_NIGHTLY_DSN \
        --volume "$PWD:$PWD" --workdir "$PWD" datadouble:nightly \
        --json plan --spec "$spec") || status=$?
    if [ "$status" = 0 ] || [ "$(jq -r .error <<< "$out")" != outside_boundary ]; then
        echo "a GitHub-hosted machine marked inside was not refused: $out" >&2
        return 1
    fi
}

case "${1:-}" in
    source) source_database ;;
    pipeline) pipeline ;;
    refusal) refusal ;;
    *) sed -n '/^#   run.sh/p' "$0" >&2; exit 2 ;;
esac
