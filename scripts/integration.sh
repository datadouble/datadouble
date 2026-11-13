#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The integration suite, as `make test-integration` runs it in the test
# container. Datadouble runs as the user an engine's copies are served as
# (decision 86), so each engine's tests run as that user and group: 999:999
# for the official PostgreSQL, MySQL and MongoDB images (and everything that
# needs no engine), 10001:10001 for SQL Server, 54321:54321 for Oracle. The
# container starts as root only to drop to each; the groups' coverage is
# combined into one report, which must reach 100%.
set -uo pipefail
cd /src

export PYTHONDONTWRITEBYTECODE=1
install -d -m 1777 /reports
rm -f /reports/.coverage*
status=0

as() {
    local who=$1 name=$2
    shift 2
    install -d -o "${who%:*}" -g "${who#*:}" "/tmp/home-$name"
    setpriv --reuid="${who%:*}" --regid="${who#*:}" --clear-groups \
        env HOME="/tmp/home-$name" USER="test-$name" COVERAGE_FILE="/reports/.coverage.$name" \
        /src/.venv/bin/pytest -p no:cacheprovider --cov --cov-report= --cov-fail-under=0 "$@" \
        || status=1
}

# Every test path but the engines' own, which run as their owners. pytest
# ignores --ignore when it falls back to its configured test paths, so the
# default group names its paths.
mapfile -t default < <(/src/.venv/bin/python -c '
import tomllib
paths = tomllib.load(open("pyproject.toml", "rb"))["tool"]["pytest"]["ini_options"]["testpaths"]
print("\n".join(p for p in paths if p not in {"adapters/mssql/tests", "adapters/oracle/tests"}))
')
as 999:999 default "${default[@]}" "$@"
as 10001:10001 mssql adapters/mssql/tests "$@"
if [ -d adapters/oracle/tests ]; then
    as 54321:54321 oracle adapters/oracle/tests "$@"
fi

/src/.venv/bin/coverage combine --data-file=/reports/.coverage /reports/.coverage.* \
    && /src/.venv/bin/coverage xml --data-file=/reports/.coverage -o /reports/coverage.xml \
    && /src/.venv/bin/coverage report --data-file=/reports/.coverage \
    || status=1
exit $status
