# datadouble-mongodb

The MongoDB engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of a replica set or sharded cluster through a
snapshot session, writes the chosen documents in `mongodump`'s format, and
builds working databases as throwaway `mongod` servers. Golden images are a
data directory (`physical`) or a `mongodump` archive (`logical`). Servers,
`mongodump` and `mongorestore` run in the official MongoDB image of the
source's exact version, through the spec's container runtime.

```yaml
source:
  engine: mongodb
  connection:
    uri:                     # a mongodb:// or mongodb+srv:// URI, by reference
      env: CINEMA_URI
    database: cinema
subset:
  seeds:
    users: '{"created": {"$lte": ":as_of"}}'   # JSON filters; ":as_of" is the plan's anchor
  virtual_fks:
    comments.user_id -> users: {}
  unselected: none   # collections nothing reaches, such as comments here, are copied empty
```

MongoDB declares no references, so every reference is virtual; subsets use
the keys strategy. Snapshot reads must finish within the server's
`minSnapshotHistoryWindowInSeconds` (300 seconds by default).

## Requirements

The adapter reports none: MongoDB loads nothing into its server, and copies
get the collections, their options and their ordinary indexes. Atlas Search
and Vector Search indexes live outside the database and are not copied.
