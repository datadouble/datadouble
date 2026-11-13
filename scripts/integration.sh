#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The integration suite's groups (tests/integration-groups.txt).
#
#   integration.sh groups             the group names, in order
#   integration.sh jobs               CI's jobs, one per shard, as a JSON matrix
#   integration.sh services RUN...    the Compose services the runs need
#   integration.sh test RUN...        run the tests (in the test container, as root)
#   integration.sh report             combine the runs' coverage, failing under 100%,
#                                     and their test durations
#
# A RUN is a group (all its tests) or GROUP/K, the Kth of its shards.
#
# Datadouble runs as the user an engine's copies are served as (decision 86),
# so the test container starts as root only to run each group as its user,
# with the Docker socket's group too when the tests drive the host's daemon.
# Each run writes its own coverage data to /reports/.coverage.GROUP (or
# .coverage.GROUP-K for a shard) and its tests' durations to
# /reports/durations.GROUP.json, which `report` combines, here or in CI after
# every job. Committed to tests/integration-durations.json, CI's durations
# balance the shards.
set -uo pipefail
cd "$(dirname "$0")/.."

table=tests/integration-groups.txt
# Seconds each test took in CI, which balance the shards (scripts/shard.py).
durations=tests/integration-durations.json

rows() {
    grep -v '^#' "$table" | grep -v '^[[:space:]]*$'
}

field() {
    # The FIELD-th column (1-5) of GROUP's row, trimmed.
    rows | awk -F'|' -v group="$1" -v column="$2" '
        { name = $1; gsub(/^ +| +$/, "", name) }
        name == group { value = $column; gsub(/^ +| +$/, "", value); print value; found = 1 }
        END { if (!found) exit 1 }'
}

known() {
    # Check a RUN: a group, or GROUP/K with K one of its shards.
    local group=${1%%/*} shard
    field "$group" 1 > /dev/null || { echo "no integration group named $group" >&2; exit 2; }
    if [ "$1" != "$group" ]; then
        shard=${1#*/}
        if ! [[ $shard =~ ^[1-9][0-9]*$ ]] || [ "$shard" -gt "$(field "$group" 3)" ]; then
            echo "group $group has no shard $shard" >&2
            exit 2
        fi
    fi
}

run_tests() {
    local run=$1 group=${1%%/*} name=${1/\//-} who uid gid groups=--clear-groups shard=()
    who=$(field "$group" 2)
    uid=${who%:*} gid=${who#*:}
    if [ "$run" != "$group" ]; then
        shard=(--shard "${run#*/}/$(field "$group" 3)")
        if [ -f "$durations" ]; then
            shard+=(--shard-durations "$durations")
        fi
    fi
    if [ -S /var/run/docker.sock ]; then
        groups="--groups=$(stat -c %g /var/run/docker.sock)"
    fi
    install -d -o "$uid" -g "$gid" "/tmp/home-$name"
    rm -f "/reports/.coverage.$name" "/reports/durations.$name.json"
    setpriv --reuid="$uid" --regid="$gid" "$groups" \
        env HOME="/tmp/home-$name" USER="test-$group" COVERAGE_FILE="/reports/.coverage.$name" \
        DATADOUBLE_TEST_REPORTS=/reports \
        /src/.venv/bin/pytest -p no:cacheprovider --cov --cov-report= --cov-fail-under=0 \
        --durations=15 --durations-min=5 --record-durations "/reports/durations.$name.json" \
        "${shard[@]}" $(field "$group" 5)
}

case "${1:-}" in
    groups)
        rows | awk -F'|' '{ name = $1; gsub(/^ +| +$/, "", name); print name }'
        ;;
    jobs)
        # {"include": [{"name": "mysql 1/2", "run": "mysql/1", "slug": "mysql-1"}, ...]};
        # a group of one shard is one job named for the group.
        rows | awk -F'|' '
            { name = $1; gsub(/^ +| +$/, "", name); count = $3 + 0
              for (k = 1; k <= count; k++) {
                  if (count == 1) job = sprintf("{\"name\": \"%s\", \"run\": \"%s\", \"slug\": \"%s\"}", name, name, name)
                  else job = sprintf("{\"name\": \"%s %d/%d\", \"run\": \"%s/%d\", \"slug\": \"%s-%d\"}", name, k, count, name, k, name, k)
                  jobs = jobs (jobs == "" ? "" : ", ") job
              } }
            END { printf "{\"include\": [%s]}\n", jobs }'
        ;;
    services)
        shift
        for run in "$@"; do known "$run"; done
        for run in "$@"; do
            field "${run%%/*}" 4 | tr ' ' '\n'
        done | awk 'NF && !seen[$0]++' | tr '\n' ' '
        echo
        ;;
    test)
        shift
        for run in "$@"; do known "$run"; done
        install -d -m 1777 /reports
        status=0
        for run in "$@"; do
            run_tests "$run" || status=1
        done
        exit $status
        ;;
    report)
        # Run from the repository on the host or in the container: relative
        # file names (relative_files) let either combine the runs' data.
        reports=${REPORTS:-/reports}
        coverage=${COVERAGE:-/src/.venv/bin/coverage}
        python=${PYTHON:-/src/.venv/bin/python}
        # The gate is the report's, which lists the lines no test ran; the
        # XML report is written whatever the total.
        $python scripts/shard.py merge "$reports/integration-durations.json" "$reports"/durations.*.json \
            && $coverage combine --keep --data-file="$reports/.coverage" "$reports"/.coverage.* \
            && $coverage xml --data-file="$reports/.coverage" -o "$reports/coverage.xml" --fail-under=0 \
            && $coverage report --data-file="$reports/.coverage"
        ;;
    *)
        sed -n '/^#   integration.sh/p' "$0" >&2
        exit 2
        ;;
esac
