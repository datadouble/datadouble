# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The Sakila fixture files, nothing else. The Compose fixtures service mounts
# an empty volume at /fixtures, which Docker fills from this image; database
# containers then mount that volume read-only and bulk-load from it.
FROM busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
COPY data /fixtures/sakila
COPY LICENSE.txt /fixtures/sakila/LICENSE.txt
CMD ["true"]
