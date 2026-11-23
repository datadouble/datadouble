# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Caddy serving the exports volume over HTTPS with basic auth, for the http
# store's integration test: `promote` writes a filesystem export store into
# the volume, and `fetch` reads it back through this server. The certificate
# comes from Caddy's own internal CA, whose root the start script copies to
# the web-ca volume for the tests to trust.

FROM caddy:2.11.9@sha256:4851e54a46b206517c574821bb5cc67f170d2156041e9fbc4c9193b45e84a75b

COPY --chmod=0644 Caddyfile /etc/caddy/Caddyfile
COPY --chmod=0755 start.sh /usr/local/bin/start-datadouble.sh
ENTRYPOINT ["/usr/local/bin/start-datadouble.sh"]
