#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The integration suite's groups (tests/integration-groups.txt).
#
#   integration.sh groups             the group names, in order
#   integration.sh services GROUP...  the Compose services the groups need
#   integration.sh test GROUP...      run the groups' tests (in the test container, as root)
#   integration.sh report             combine the groups' coverage; fail under 100%
#
# Datadouble runs as the user an engine's copies are served as (decision 86),
# so the test container starts as root only to run each group as its user,
# with the Docker socket's group too when the tests drive the host's daemon.
# Each group writes its own coverage data to /reports/.coverage.GROUP, which
# `report` combines, here or in CI after every group's job.
set -uo pipefail
cd "$(dirname "$0")/.."

table=tests/integration-groups.txt

rows() {
    grep -v '^#' "$table" | grep -v '^[[:space:]]*$'
}

field() {
    # The FIELD-th column (1-4) of GROUP's row, trimmed.
    rows | awk -F'|' -v group="$1" -v column="$2" '
        { name = $1; gsub(/^ +| +$/, "", name) }
        name == group { value = $column; gsub(/^ +| +$/, "", value); print value; found = 1 }
        END { if (!found) exit 1 }'
}

known() {
    field "$1" 1 > /dev/null || { echo "no integration group named $1" >&2; exit 2; }
}

run_group() {
    local group=$1 who uid gid groups=--clear-groups
    known "$group"
    who=$(field "$group" 2)
    uid=${who%:*} gid=${who#*:}
    if [ -S /var/run/docker.sock ]; then
        groups="--groups=$(stat -c %g /var/run/docker.sock)"
    fi
    install -d -o "$uid" -g "$gid" "/tmp/home-$group"
    setpriv --reuid="$uid" --regid="$gid" "$groups" \
        env HOME="/tmp/home-$group" USER="test-$group" COVERAGE_FILE="/reports/.coverage.$group" \
        /src/.venv/bin/pytest -p no:cacheprovider --cov --cov-report= --cov-fail-under=0 \
        $(field "$group" 4)
}

case "${1:-}" in
    groups)
        rows | awk -F'|' '{ name = $1; gsub(/^ +| +$/, "", name); print name }'
        ;;
    services)
        shift
        for group in "$@"; do known "$group"; done
        for group in "$@"; do
            field "$group" 3 | tr ' ' '\n'
        done | awk 'NF && !seen[$0]++' | tr '\n' ' '
        echo
        ;;
    test)
        shift
        for group in "$@"; do known "$group"; done
        install -d -m 1777 /reports
        status=0
        for group in "$@"; do
            rm -f "/reports/.coverage.$group"
            run_group "$group" || status=1
        done
        exit $status
        ;;
    report)
        # Run from the repository on the host or in the container: relative
        # file names (relative_files) let either combine the groups' data.
        reports=${REPORTS:-/reports}
        coverage=${COVERAGE:-/src/.venv/bin/coverage}
        # The gate is the report's, which lists the lines no test ran; the
        # XML report is written whatever the total.
        $coverage combine --keep --data-file="$reports/.coverage" "$reports"/.coverage.* \
            && $coverage xml --data-file="$reports/.coverage" -o "$reports/coverage.xml" --fail-under=0 \
            && $coverage report --data-file="$reports/.coverage"
        ;;
    *)
        sed -n '/^#   integration.sh/p' "$0" >&2
        exit 2
        ;;
esac
