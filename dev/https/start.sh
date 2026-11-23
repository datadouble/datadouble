#!/bin/sh
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Let every test user write to the exports volume, hash the password for
# basic auth, and start Caddy. Once Caddy has issued its certificate, copy its
# internal CA's root to the web-ca volume and mark the server ready for the
# health check.
set -e
chmod 1777 /srv/exports
EXPORT_PASSWORD_HASH=$(caddy hash-password --plaintext "$EXPORT_PASSWORD")
export EXPORT_PASSWORD_HASH
(
  root=/data/caddy/pki/authorities/local/root.crt
  until [ -f "$root" ] && [ -f /data/caddy/certificates/local/web/web.crt ]; do sleep 0.2; done
  cp "$root" /ca/root.crt.partial
  chmod 0644 /ca/root.crt.partial
  mv /ca/root.crt.partial /ca/root.crt
  touch /tmp/ready
) &
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
