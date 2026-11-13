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
    url: {env: SESSIONS_URL}   # a redis:// or rediss:// URL, by reference
    databases: [0]
    patterns: ["session:{}"]   # keys inference would group wrongly
subset:
  seeds:
    user:{}: "1*"              # seeds and `when` clauses are globs over _key
  virtual_fks:
    user:{}:sessions._key -> user:{}: {}   # a reference in a key's name
    order:{}.user_id -> user:{}: {}        # a reference in a hash field
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
