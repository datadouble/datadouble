# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Elasticsearch 9.5 as a single node, seeded with generated indices derived
# from Pagila: join keys across indices, a nested field, custom routing, an
# index with unmapped fields, and the awkward cases the adapter must handle.
# The node is seeded on its first start.

FROM elasticsearch:9.5.5@sha256:b7b0f24e9b5e90656ffe69804b3a48907499ab2b9cd67f02b7af3aff1b026a89

COPY --chmod=0755 mappings.sh generate.awk /seed/
COPY --chmod=0755 start.sh /usr/local/bin/start-datadouble.sh
ENTRYPOINT ["/bin/tini", "--", "/usr/local/bin/start-datadouble.sh"]
