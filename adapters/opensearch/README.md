# datadouble-opensearch

The OpenSearch engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It is the [Elasticsearch adapter](https://github.com/datadouble/datadouble/tree/main/adapters/elasticsearch)
serving OpenSearch: the same tables, subsets, images and rules, described
there, with OpenSearch's image, security plugin and point-in-time API. The
source is OpenSearch 2.4 or later, the first version with points in time.

```yaml
sources:
  app:
    engine: opensearch
    connection:
      url:                         # http[s]://[user:password@]host[:port], by reference
        env: SEARCH_URL
      indices: ["*", "-logs-*"]    # index patterns; the default is every open index
```

Working nodes and copies run in the official `opensearchproject/opensearch`
image of the source's exact version, with the security plugin's
demonstration configuration over plain HTTP. Datadouble and consumers sign
in as `admin`, whose password a node or copy gets at its first start and
keeps in its data directory.

## Plugins

Plugins are requirements as for
[Elasticsearch](https://github.com/datadouble/datadouble/tree/main/adapters/elasticsearch#plugins),
listed in the image with `opensearch-plugin list`. The official image
bundles the project's plugins (security, k-NN, alerting and the rest) at the
source version's own release, so a source running the same bundle needs
nothing more; any others need an image built `FROM` the official one.

The catalog leaves out the indices plugins write for themselves: those
starting with a dot, and query insights' daily `top_queries-*` indices,
which its mapping's `_meta` marks.
