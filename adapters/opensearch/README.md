# datadouble-opensearch

The OpenSearch engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It is the [Elasticsearch adapter](https://github.com/datadouble/datadouble/tree/main/adapters/elasticsearch)
serving OpenSearch: the same tables, subsets, images and rules, described
there, with OpenSearch's image, security plugin and point-in-time API. The
source is OpenSearch 2.4 or later, the first version with points in time.

```yaml
source:
  engine: opensearch
  connection:
    url: {env: SEARCH_URL}       # http[s]://[user:password@]host[:port], by reference
    indices: ["*", "-logs-*"]    # index patterns; the default is every open index
```

Working nodes and copies run in the official `opensearchproject/opensearch`
image of the source's exact version, with the security plugin's
demonstration configuration over plain HTTP. Datadouble and consumers sign
in as `admin`, whose password a node or copy gets at its first start and
keeps in its data directory.
