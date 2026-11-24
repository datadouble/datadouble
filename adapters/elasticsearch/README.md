# datadouble-elasticsearch

The Elasticsearch engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

Each index is a table keyed by `_id`, and its columns are the field paths its
mapping declares: `address.city` for an object's field, `actors.actor_id` for
a field of nested documents. The extract reads through one point in time over
every index, so it sees them all as they were when it started. Working
databases are throwaway single-node clusters in the official Elasticsearch
image of the source's exact version, through the spec's container runtime. A
golden image is a snapshot repository (`physical`) or each index's mappings,
settings and documents as NDJSON (`logical`).

```yaml
sources:
  app:
    engine: elasticsearch
    connection:
      url:                         # http[s]://[user:password@]host[:port], by reference
        env: SEARCH_URL
      api_key:                     # optional; sent instead of the URL's credentials
        env: SEARCH_KEY
      indices: ["*", "-logs-*"]    # index patterns; the default is every open index
subset:
  seeds:
    customers: '{"range": {"created": {"lte": ":as_of"}}}'   # Query DSL; ":as_of" is the plan's anchor
  virtual_fks:
    rentals.customer_id -> customers: {}                  # a reference to a document's _id
    payments.rental_id -> rentals.rental_id: {}           # a reference to another field
  unselected: none   # indices nothing reaches, such as rentals here, are copied empty
```

Elasticsearch declares no references, so every reference is virtual, and
subsets use the keys strategy. References must be `keyword`, numeric or `_id`
fields: no query matches a `text` field's value exactly.

An index whose mapping lets documents hold undeclared fields (`dynamic:
false` or `runtime`) also gets a column for each such field in its first
`sample` documents (1000 by default). The extract fails if a document it
copies holds a field the catalog does not list, so strict mode covers every
field that leaves the source. Indices that keep no `_source`, or only part of
it, cannot be copied, and data streams are not copied: leave them out of
`indices`. Indices whose names start with a dot belong to the server and are
never tables.

An index keeps its mappings, aliases and the settings that describe it
(shards, analysis, sorting, mode). Settings that tie it to the source
cluster are left out: allocation, lifecycle policies, ingest pipelines and
write blocks. Copies have no replicas.

Anonymization rewrites fields wherever a document holds them, in objects and
arrays alike. Expression rules are Painless scripts, run with
`_update_by_query` against each document (`ctx._source`) with
`params.column` naming the column; `where` is a Query DSL object. `_id` and
`_routing` are copied as they are, and no rule changes them: keep personal
data out of document IDs and routing values, or out of copies. Verify
assertions are `{"index": "...", "query": {...}}`, failing when they match
any document.

The source must be Elasticsearch 7.17 or later. Working nodes have a 512 MB
heap; set `sources.<name>.server.heap` (such as `2g`) for larger subsets.

## Plugins

Every plugin installed on the source's nodes (`GET _nodes/plugins`) is a
requirement at its version, such as `analysis-icu 9.5.5`. Reading them
needs the `monitor` cluster privilege, as well as read access to the
indices. Before reading the source, a build lists the working image's
plugins (`elasticsearch-plugin list`) and fails with `requirement_unmet` if
one is missing. The official image has none installed, so a source with
analysis or mapper plugins needs an image that adds them, built `FROM` the
official image of the source's version:

```dockerfile
FROM elasticsearch:9.5.5
RUN elasticsearch-plugin install --batch analysis-icu analysis-kuromoji
```

Then name it in `sources.<name>.image`. A plugin the copies can go without, such
as a repository plugin, can be left out with `sources.<name>.omit`; an index whose
mapping uses an omitted plugin's analyzer or field type fails to load, so
exclude it too.
