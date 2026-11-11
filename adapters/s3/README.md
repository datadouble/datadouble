# datadouble-s3

The S3 storage adapter for [Datadouble](https://datadouble.tightlinesoftware.com/):
golden images and their image summaries in an S3 bucket, or any service with
an S3-compatible API.

```yaml
store:
  - s3:
      bucket: golden-images
      prefix: shop              # optional
      region: us-east-1         # optional
      endpoint_url: https://s3.internal.example   # optional, for S3-compatible services
```

Credentials come from the usual AWS sources (environment, shared files, an
instance or workload role); they never appear in the spec.
