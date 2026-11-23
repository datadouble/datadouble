# datadouble-redis

The Redis engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

Each key pattern is a table: `user:42` belongs to `user:{}` and
`user:42:sessions` to `user:{}:sessions`, and a table's key, `_key`, holds
the values its `{}` stand for. Its other columns are what its keys hold:
`value` for strings, the fields of hashes and stream entries, `members` for
lists, sets and sorted sets, and `scores` for sorted sets.

The extract reads only the chosen keys, each with `DUMP`; Redis has no
snapshot, so each key is copied as it was when read. Working databases are
throwaway `redis-server` processes in the official Redis image of the
source's exact version, through the spec's container runtime, and a golden
image is the `dump.rdb` they save (`physical`; there is no logical format).
Copies keep no expiry.

```yaml
source:
  engine: redis
  connection:
    url:                       # a redis:// or rediss:// URL, by reference
      env: SESSIONS_URL
    databases: [0]
    patterns: ["session:{}"]   # keys inference would group wrongly
subset:
  seeds:
    user:{}: "1*"              # seeds and `when` clauses are globs over _key
  virtual_fks:
    user:{}:sessions._key -> user:{}: {}   # a reference in a key's name
    order:{}.user_id -> user:{}: {}        # a reference in a hash field
  unselected: none   # patterns nothing reaches, such as order:{} here, are copied empty
```

Key segments that are not names (numbers, UUIDs, long hex strings, anything
with characters other than letters, digits, `_` and `-`) become `{}`. When
the keys under one prefix have more distinct literal segments than a name
would (64 for a key's first segment, 16 after it), planning fails until a
pattern is declared for them, since table names leave production in the
catalog lock. Declare patterns for any key whose segments are data.

Anonymization rewrites hash fields, strings and the elements of lists, sets,
sorted sets and streams; expression rules are Lua, run for each key with
`KEYS[1]` the key and `ARGV[1]` the column. `_key` is a key column, which no
rule changes; keep data out of key names, or out of copies. Verify assertions
are Lua scripts returning a count.

## Modules

Every module the source has loaded (`MODULE LIST`) is a requirement at its
version, such as `module ReJSON 81000`. Redis 8's official image bundles
JSON, search, time series and probabilistic structures in
`/usr/local/lib/redis/modules`, and every server Datadouble starts loads
every module there, as the image's own entrypoint does, so a source on the
same release needs nothing more. Other modules need an image built `FROM`
the official one with their libraries in that directory; Valkey and managed
services that bundle different modules need an image that matches.

A key of a module's type, such as a JSON document, gets a `value` column
typed with the module's type (`ReJSON-RL`) and the kind `other`. Datadouble
cannot read or rewrite those values, so strict mode asks what to do with the
column: exclude the keys, write an expression rule (Lua can call the
module's commands, such as `JSON.SET`), or accept the column in the lock
once someone has checked it holds no personal data.
