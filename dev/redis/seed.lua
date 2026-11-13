-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- A generated keyspace, the same at every run. Database 0 holds a small
-- shop: users (hashes) with their session sets, sessions (strings named by
-- tokens that look like names, so their pattern must be declared), orders
-- (hashes) with item lists naming products, products (strings), a
-- leaderboard (a sorted set) and an audit stream. Database 1 holds a cache.
-- Every name, address and token is invented.

local first = {"Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus", "Hal", "Ivy", "Jo"}
local last = {"Smith", "Jones", "Brown", "Lee", "Patel", "Garcia", "Kim", "Nguyen"}
local cities = {"Springfield", "Riverton", "Lakeside", "Hillview", "Fairmont"}

redis.call("SELECT", 0)
redis.call("FLUSHDB")
for id = 1, 120 do
  local given, family = first[id % #first + 1], last[id % #last + 1]
  redis.call("HSET", "user:" .. id,
    "name", given .. " " .. family,
    "email", string.lower(given) .. "." .. string.lower(family) .. id .. "@example.com",
    "phone", string.format("555-01%02d", id % 100),
    "city", cities[id % #cities + 1])
  if id % 3 == 0 then
    for n = 1, 2 do
      local token = "tok-" .. id .. "-" .. n
      redis.call("SADD", "user:" .. id .. ":sessions", token)
      redis.call("SET", "session:" .. token, tostring(id))
    end
  end
end
for id = 1, 40 do
  redis.call("SET", "product:" .. id, "Product " .. id)
end
for id = 1, 300 do
  local user = id % 120 + 1
  redis.call("HSET", "order:" .. id,
    "user_id", tostring(user),
    "total", string.format("%d.%02d", id % 90 + 10, id % 100),
    "placed", string.format("2024-%02d-%02dT10:00:00Z", id % 12 + 1, id % 28 + 1))
  for n = 1, id % 3 + 1 do
    redis.call("RPUSH", "order:" .. id .. ":items", tostring((id * 7 + n) % 40 + 1))
  end
end
for id = 1, 120, 7 do
  redis.call("ZADD", "leaderboard", id * 13 % 1000, tostring(id))
end
for id = 1, 60 do
  redis.call("XADD", "audit", "*", "user_id", tostring(id % 120 + 1),
    "action", (id % 2 == 0) and "login" or "logout",
    "ip", "192.0.2." .. (id % 250 + 1))
end

-- Awkward cases: an empty string, bytes that are not UTF-8, field names with
-- dots and spaces, a key that expires, a singleton key, and a table a spec
-- is expected to exclude.
redis.call("SET", "note:1", "")
redis.call("SET", "note:2", "caf\233 \255\254")
redis.call("HSET", "profile:1", "first.name", "Ada", "favourite colour", "teal")
redis.call("SET", "flash:1", "temporary", "EX", 86400)
redis.call("SET", "config", "{\"theme\": \"dark\"}")
for id = 1, 5 do
  redis.call("SET", "maint:run:" .. id, "2024-01-0" .. id)
end

redis.call("SELECT", 1)
redis.call("FLUSHDB")
for id = 1, 30 do
  redis.call("SET", "cache:" .. id, "cached page " .. id, "EX", 86400)
end
redis.call("SELECT", 0)
return "seeded"
