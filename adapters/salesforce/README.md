# datadouble-salesforce

The Salesforce engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads an org through its REST, Tooling and Bulk API 2.0 interfaces and
builds everything else locally: working copies are SQLite databases with a
table per object, anonymization runs there, and golden images are logical,
each object's records as JSON lines with a description of its fields.

```yaml
source:
  engine: salesforce
  connection:
    instance_url:                               # by reference, like the token
      env: SHOP_SF_INSTANCE_URL
    access_token:
      env: SHOP_SF_ACCESS_TOKEN
    api_version: "62.0"                         # the default
    objects: [Account, Contact, Opportunity, "Acme__*"]   # optional
    hidden: [Contact.Pronouns]                  # optional: fields copies may go without
subset:
  seeds:
    Account: "Industry = 'Technology' AND CreatedDate <= :as_of"   # SOQL; :as_of is the anchor
  follow:
    down:
      - Account -> Contact
anonymize:
  Contact:
    Email:
      faker: email
      unique: true
    LastName:
      faker: last_name
```

The token is an OAuth access token for a user who can read the objects and
the Tooling API (the "View Setup and Configuration" permission), which lists
the installed packages and every field of each object.

## Fields field-level security hides

Salesforce leaves fields that field-level security hides from the user out of
the object's description, so a copy would lack them without any error. The
catalog finds them instead: a field that the Tooling API's `FieldDefinition`
lists, that a profile or permission set grants (`FieldPermissions`), but that
the description leaves out is hidden from this user. Fields granted to no one
are internal, or belong to features the org does not use, and are never
described anywhere, so they do not count. A field a managed package
maintains, such as a rollup's result, is often hidden this way.

The catalog, and so the plan, fails before anything is extracted, naming each
hidden field. Give the user read access to them, or list them in `hidden`
(as `Object.Field`) to accept that copies go without them.

## The catalog

Each object is a table, its fields the columns and `Id` the key. The catalog
lists the objects that can be queried, read, created and replicated. It leaves
out history, feed, share and change-event objects, custom metadata types,
platform events, and external and big objects; `objects` narrows it further,
with `*`, `?` and `[...]` patterns. Compound address and location fields are
left out, since their parts are fields too, and so are base64 fields, which
the Bulk API cannot export.

A reference field that points at one object in the catalog is a foreign key
to its `Id`. Polymorphic references, such as a task's `WhoId` and `WhatId`,
can point at several objects and are not; declare them as virtual foreign
keys, with a `when` on the type where needed, if a subset must follow them.

## Consistency

An org has no snapshots. Each query and export sees the records as they are
when it runs, so a record changed during the extract may appear before or
after the change, and a child created mid-way may name a parent the extract
did not see. Extract outside busy hours, and check references with the
`references` verification. The extract fails if an object's fields changed
since the plan.

## Subsets

Seeds and `when` clauses are SOQL conditions. Subsets use the keys strategy:
Salesforce's semi-joins go only one level deep and cannot be combined with
`OR`, so nested selections are not offered. Long `IN` lists are split across
queries, and records chosen twice are written once.

Anonymization takes the faker, hash, format-preserving and null rules;
expression rules and hooks are not offered. Assertions are SOQL, run against
a restored copy: one object, plain fields, comparisons, `LIKE`, `IN`, `AND`,
`OR`, `NOT`, `ORDER BY` and `LIMIT`. As in Salesforce, comparing text
ignores case.

## Requirements

The adapter reports each installed package (`InstalledSubscriberPackage`) as
a `package` requirement, by namespace, at its version. Copies are local
databases that hold a package's objects and fields as ordinary tables and
columns, so they need none of them; the build does not check them, and the
lock records them so strict mode notices a package installed, removed or
upgraded.

## Images

Images are `logical` only, and there is no server to start, so they cannot be
replicated as running databases yet. Loading an image's records into an org
(a sandbox or scratch org) is planned.

## The fake org

`datadouble_salesforce.fake` serves a SQLite file over the same APIs, so the
whole adapter runs without an org:

```yaml
source:
  engine: salesforce
  connection:
    fake: /path/to/org.sqlite
```

`FakeOrg` builds the file (objects, fields, records and packages) and
`sample_org(path)` writes a small one with made-up records. The fake answers
the SOQL assertions use, and has no snapshots either.

## Recorded cassettes

`datadouble_salesforce.cassettes` records the adapter's HTTP interactions,
scrubs them and replays them. The scrubber keeps only the headers replay
needs and replaces access tokens, session IDs, the instance URL and other
Salesforce hosts, and org IDs. To re-record the committed cassette against a
development org:

```sh
sf org login web --alias datadouble-dev
make record-salesforce ORG=datadouble-dev
```
