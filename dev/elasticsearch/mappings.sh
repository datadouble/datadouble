# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# The indices, created by start.sh with its `call` function. Every index has
# one shard and no replicas, as a single node needs.

index() {
  call -o /dev/null -H 'content-type: application/json' -X PUT "http://127.0.0.1:9200/$1" -d "{
    \"settings\": {\"number_of_shards\": 1, \"number_of_replicas\": 0},
    \"mappings\": $2
  }"
}

address='{"properties": {"address": {"type": "text"}, "district": {"type": "keyword"},
  "city": {"type": "keyword"}, "postal_code": {"type": "keyword"}, "phone": {"type": "keyword"}}}'
customer="{\"properties\": {
  \"customer_id\": {\"type\": \"long\"}, \"store_id\": {\"type\": \"long\"},
  \"first_name\": {\"type\": \"keyword\"}, \"last_name\": {\"type\": \"keyword\"},
  \"email\": {\"type\": \"keyword\"}, \"active\": {\"type\": \"boolean\"},
  \"create_date\": {\"type\": \"date\"}, \"last_update\": {\"type\": \"date\"},
  \"address\": $address}}"

index stores "{\"properties\": {\"store_id\": {\"type\": \"long\"},
  \"manager_staff_id\": {\"type\": \"long\"}, \"address\": $address}}"
# A staff member's manager is another staff member; stores and staff refer
# to each other.
index staff '{"properties": {"staff_id": {"type": "long"}, "store_id": {"type": "long"},
  "manager_id": {"type": "long"}, "first_name": {"type": "keyword"},
  "last_name": {"type": "keyword"}, "email": {"type": "keyword"},
  "username": {"type": "keyword"}, "active": {"type": "boolean"}}}'
index customers "$customer"
# An index with no documents.
index archived_customers "$customer"
index films '{"properties": {"film_id": {"type": "long"},
  "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
  "description": {"type": "text"}, "release_year": {"type": "integer"},
  "rental_rate": {"type": "scaled_float", "scaling_factor": 100},
  "length": {"type": "integer"}, "rating": {"type": "keyword"},
  "categories": {"type": "keyword"}, "special_features": {"type": "keyword"},
  "actors": {"type": "nested", "properties": {"actor_id": {"type": "long"},
    "first_name": {"type": "keyword"}, "last_name": {"type": "keyword"}}}}}'
# A rental's customer_id is a customer's _id, as text.
index rentals '{"properties": {"rental_id": {"type": "long"},
  "rental_date": {"type": "date"}, "return_date": {"type": "date"},
  "customer_id": {"type": "keyword"}, "film_id": {"type": "long"},
  "staff_id": {"type": "long"}}}'
# Payments are routed by customer, and have generated IDs.
index payments '{"_routing": {"required": true}, "properties": {
  "payment_id": {"type": "long"}, "rental_id": {"type": "long"},
  "customer_id": {"type": "long"}, "staff_id": {"type": "long"},
  "amount": {"type": "scaled_float", "scaling_factor": 100},
  "payment_date": {"type": "date"}}}'
# A review is about a film or a customer: a polymorphic reference.
index reviews '{"properties": {"review_id": {"type": "long"},
  "subject_type": {"type": "keyword"}, "subject_id": {"type": "keyword"},
  "stars": {"type": "byte"}, "body": {"type": "text"}, "posted": {"type": "date"},
  "reviewer": {"properties": {"name": {"type": "keyword"}, "email": {"type": "keyword"}}}}}'
# Events keep fields their mapping does not list.
index events '{"dynamic": false, "properties": {"event_id": {"type": "long"},
  "type": {"type": "keyword"}, "at": {"type": "date"}}}'
