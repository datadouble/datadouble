#!/bin/sh
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Start Redis without persistence and with the image's modules, as the
# image's own entrypoint loads them, seed it, then mark it ready for the
# health check.
set -e
(
  until redis-cli ping >/dev/null 2>&1; do sleep 0.2; done
  redis-cli --eval /seed/seed.lua >/dev/null
  touch /tmp/ready
) &
set --
for module in /usr/local/lib/redis/modules/*.so; do
  [ -e "$module" ] && set -- "$@" --loadmodule "$module"
done
exec redis-server --save "" --appendonly no --protected-mode no "$@"
