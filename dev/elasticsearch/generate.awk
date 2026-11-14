# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Writes the generated documents as one _bulk request body. The data is
# deterministic and invented, shaped like Pagila's: every name, address and
# contact detail is made up.

function action(name, id, routing) {
  printf "{\"index\": {\"_index\": \"%s\"", name
  if (id != "") printf ", \"_id\": \"%s\"", id
  if (routing != "") printf ", \"routing\": \"%s\"", routing
  print "}}"
}

function day(n, seconds) {
  return strftime("%Y-%m-%dT%H:%M:%SZ", BASE + n * 86400 + seconds, 1)
}

function pick(list, n,    parts, count) {
  count = split(list, parts, " ")
  return parts[n % count + 1]
}

function address(n) {
  return sprintf("{\"address\": \"%d %s Street\", \"district\": \"%s\", \"city\": \"%s\", " \
    "\"postal_code\": \"%05d\", \"phone\": \"(617) 555-%04d\"}", \
    n, pick(STREETS, n), pick(DISTRICTS, n), pick(CITIES, n), 10000 + n, 1000 + n % 9000)
}

BEGIN {
  BASE = 1653350400  # 2022-05-24
  FIRST = "Ada Bo Cy Di Ed Flo Gus Hal Ivy Jo Kit Lu Max Nell Oz Pip"
  LAST = "Archer Baker Carter Dalton Ellis Foster Grant Hughes Irwin Jarvis"
  STREETS = "Elm Oak Pine Maple Cedar Birch Willow Ash"
  DISTRICTS = "North South East West Central"
  CITIES = "Riverton Lakeside Hillcrest Brookfield Fairview"
  WORDS = "epic quiet astounding thoughtful brisk sombre lively tender"
  RATINGS = "G PG PG-13 R NC-17"
  CATEGORIES = "Action Animation Comedy Documentary Drama Family Horror Music"
  FEATURES = "Trailers Commentaries Deleted-Scenes Behind-the-Scenes"

  # Two stores, each managed by a staff member who works there.
  for (i = 1; i <= 2; i++) {
    action("stores", i, "")
    printf "{\"store_id\": %d, \"manager_staff_id\": %d, \"address\": %s}\n", i, i, address(i)
  }
  # Staff 1 and 2 manage the stores and report to nobody; the rest report to them.
  for (i = 1; i <= 6; i++) {
    first = pick(FIRST, i + 3); last = pick(LAST, i + 5)
    action("staff", i, "")
    printf "{\"staff_id\": %d, \"store_id\": %d, ", i, (i - 1) % 2 + 1
    if (i > 2) printf "\"manager_id\": %d, ", (i - 1) % 2 + 1
    printf "\"first_name\": \"%s\", \"last_name\": \"%s\", \"email\": \"%s.%s@staff.example.com\", " \
      "\"username\": \"%s%d\", \"active\": %s}\n", first, last, tolower(first), tolower(last), \
      tolower(first), i, (i == 6 ? "false" : "true")
  }
  # Customers. Every tenth writes its address as dotted field names, which
  # Elasticsearch treats as the same fields.
  for (i = 1; i <= 300; i++) {
    first = pick(FIRST, i); last = pick(LAST, i * 7)
    action("customers", i, "")
    printf "{\"customer_id\": %d, \"store_id\": %d, \"first_name\": \"%s\", \"last_name\": \"%s\", " \
      "\"email\": \"%s.%s%d@example.com\", \"active\": %s, \"create_date\": \"%s\", ", \
      i, i % 2 + 1, first, last, tolower(first), tolower(last), i, (i % 17 ? "true" : "false"), day(i % 60, 0)
    if (i % 10 == 0)
      printf "\"address.city\": \"%s\", \"address.postal_code\": \"%05d\"}\n", pick(CITIES, i), 10000 + i
    else
      printf "\"address\": %s}\n", address(i)
  }
  # Films, with their actors as nested documents.
  for (i = 1; i <= 200; i++) {
    action("films", i, "")
    printf "{\"film_id\": %d, \"title\": \"%s %s %d\", \"description\": \"A %s story of %s\", " \
      "\"release_year\": %d, \"rental_rate\": %.2f, \"length\": %d, \"rating\": \"%s\", " \
      "\"categories\": [\"%s\", \"%s\"], \"special_features\": [\"%s\"], \"actors\": [", \
      i, toupper(substr(pick(WORDS, i), 1, 1)) substr(pick(WORDS, i), 2), pick(LAST, i), i, \
      pick(WORDS, i * 3), pick(CATEGORIES, i), 2000 + i % 25, 0.99 + (i % 3) * 2, 60 + i % 120, \
      pick(RATINGS, i), pick(CATEGORIES, i), pick(CATEGORIES, i + 3), pick(FEATURES, i)
    for (a = 0; a < 1 + i % 3; a++) {
      actor = (i * 7 + a * 13) % 120 + 1
      printf "%s{\"actor_id\": %d, \"first_name\": \"%s\", \"last_name\": \"%s\"}", \
        (a ? ", " : ""), actor, pick(FIRST, actor), pick(LAST, actor * 3)
    }
    print "]}"
  }
  # Rentals. Customers 301 to 305 do not exist: rentals left by deleted accounts.
  payment = 0
  for (i = 1; i <= 1500; i++) {
    customer = (i * 37) % 300 + 1
    if (i % 300 == 0) customer = 300 + i / 300
    staff = i % 6 + 1
    action("rentals", i, "")
    printf "{\"rental_id\": %d, \"rental_date\": \"%s\", ", i, day(i % 90, i % 86400)
    if (i % 11) printf "\"return_date\": \"%s\", ", day(i % 90 + 3, i % 86400)
    printf "\"customer_id\": \"%d\", \"film_id\": %d, \"staff_id\": %d}\n", \
      customer, (i * 13) % 200 + 1, staff
    # Returned rentals were paid for; payments are routed by customer.
    if (i % 11 && customer <= 300) {
      payment++
      action("payments", "", customer)
      printf "{\"payment_id\": %d, \"rental_id\": %d, \"customer_id\": %d, \"staff_id\": %d, " \
        "\"amount\": %.2f, \"payment_date\": \"%s\"}\n", \
        payment, i, customer, staff, 0.99 + (i % 5) * 2, day(i % 90 + 3, i % 86400)
    }
  }
  # Reviews of films and of customers.
  for (i = 1; i <= 300; i++) {
    film = i % 3
    action("reviews", "r-" i, "")
    printf "{\"review_id\": %d, \"subject_type\": \"%s\", \"subject_id\": \"%d\", \"stars\": %d, " \
      "\"body\": \"%s and %s\", \"posted\": \"%s\", " \
      "\"reviewer\": {\"name\": \"%s %s\", \"email\": \"reviewer%d@example.com\"}}\n", \
      i, (film ? "film" : "customer"), (film ? (i * 11) % 200 + 1 : (i * 7) % 300 + 1), \
      i % 5 + 1, pick(WORDS, i), pick(WORDS, i + 5), day(i % 90, 3600), \
      pick(FIRST, i + 1), pick(LAST, i + 2), i
  }
  # Events carry a client the mapping does not list.
  for (i = 1; i <= 200; i++) {
    action("events", "", "")
    printf "{\"event_id\": %d, \"type\": \"%s\", \"at\": \"%s\", " \
      "\"client\": {\"ip\": \"10.%d.%d.%d\", \"agent\": \"browser/%d\"}}\n", \
      i, (i % 4 ? "view" : "search"), day(i % 90, 60 * i), i % 7, i % 13, i % 250 + 1, i % 9
  }
}
