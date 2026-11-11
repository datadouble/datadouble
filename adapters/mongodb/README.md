# datadouble-mongodb

The MongoDB engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of a replica set or sharded cluster through a
snapshot session, writes the chosen documents in `mongodump`'s format, and
builds working databases as throwaway local `mongod` servers. Golden images
are a data directory (`physical`) or a `mongodump` archive (`logical`). The
machine running it needs `mongod`, `mongodump` and `mongorestore` of the
version images are built for.

```yaml
source:
  engine: mongodb
  connection:
    uri: {env: CINEMA_URI}   # a mongodb:// or mongodb+srv:// URI, by reference
    database: cinema
subset:
  seeds:
    users: '{"created": {"$lte": ":as_of"}}'   # JSON filters; ":as_of" is the plan's anchor
  virtual_fks:
    comments.user_id -> users: {}
```

MongoDB declares no references, so every reference is virtual; subsets use
the keys strategy. Snapshot reads must finish within the server's
`minSnapshotHistoryWindowInSeconds` (300 seconds by default).
