#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Start OpenSearch with its security plugin over plain HTTP, create and fill
# the indices once it answers (on the container's first start only), then
# mark it ready for the health check. The password is the test environment's
# own.
set -e
export OPENSEARCH_INITIAL_ADMIN_PASSWORD=Datadouble-1-Search
rm -f /tmp/ready
(
  call() { curl -sf -u "admin:$OPENSEARCH_INITIAL_ADMIN_PASSWORD" "$@"; }
  until call -o /dev/null 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s'; do
    sleep 1
  done
  if ! call -o /dev/null 'http://127.0.0.1:9200/customers'; then
    . /seed/mappings.sh
    awk -f /seed/generate.awk > /tmp/bulk.ndjson
    call -o /tmp/bulk.json -H 'content-type: application/x-ndjson' \
      -X POST 'http://127.0.0.1:9200/_bulk?refresh=true' --data-binary @/tmp/bulk.ndjson
    grep -q '"errors":false' /tmp/bulk.json
  fi
  touch /tmp/ready
) &
exec ./opensearch-docker-entrypoint.sh opensearch
