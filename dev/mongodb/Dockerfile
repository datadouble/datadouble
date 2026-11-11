# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# MongoDB 9.0 as a single-node replica set (snapshot reads need one), seeded
# with the generated "cinema" database.

FROM mongo:9.0.8@sha256:bdcd178c7ef0c0afca7f345e413f6e8960b20f65662c2db45f8380221f32338e

COPY --chmod=0644 seed.js /seed/seed.js
COPY --chmod=0755 start.sh /usr/local/bin/start-datadouble.sh
ENTRYPOINT ["/usr/local/bin/start-datadouble.sh"]
