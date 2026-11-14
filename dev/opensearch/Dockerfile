# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# OpenSearch 3.9 as a single node, seeded on its first start with the same
# generated indices as the Elasticsearch fixture (dev/elasticsearch), whose
# files it copies: Compose builds it with dev/ as the context.

FROM opensearchproject/opensearch:3.9.0@sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e

COPY --chmod=0755 elasticsearch/mappings.sh elasticsearch/generate.awk /seed/
COPY --chmod=0755 opensearch/start.sh /usr/share/opensearch/start-datadouble.sh
ENTRYPOINT ["/usr/share/opensearch/start-datadouble.sh"]
