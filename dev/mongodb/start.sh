#!/bin/sh
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Start mongod as a one-member replica set, initiate the set, wait for it to
# become primary, seed it once, then mark it ready for the health check.
set -e
(
  until mongosh --quiet --eval 'db.adminCommand("ping")' >/dev/null 2>&1; do sleep 1; done
  mongosh --quiet --eval 'try { rs.status() } catch (e) { rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongodb:27017"}]}) }' >/dev/null
  until mongosh --quiet --eval 'quit(db.hello().isWritablePrimary ? 0 : 1)' >/dev/null 2>&1; do sleep 1; done
  mongosh --quiet /seed/seed.js
  touch /tmp/ready
) &
exec mongod --replSet rs0 --bind_ip_all
