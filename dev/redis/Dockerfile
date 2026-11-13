# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Redis 8.10 seeded with a generated keyspace: every data type, references
# in key names and in values, and the awkward cases the adapter must handle.
# The data lives only in memory and is seeded again at every start.

FROM redis:8.10.3@sha256:1f88291b4c57bdf249f02eb085a8669889b2d2954e91d4de5118a5da6b78dd53

COPY --chmod=0644 seed.lua /seed/seed.lua
COPY --chmod=0755 start.sh /usr/local/bin/start-datadouble.sh
ENTRYPOINT ["/usr/local/bin/start-datadouble.sh"]
