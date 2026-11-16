# Quickstart

Build a verified, anonymized golden image from a small PostgreSQL database on
your own machine. You need Docker and a checkout of
[datadouble/datadouble](https://github.com/datadouble/datadouble).

## 1. Build the image

Datadouble runs from its container image; engine software runs in the
vendors' own images beside it (decision 72). Until the first release is
published, build the image from the checkout:

```
docker build --target runtime --tag datadouble .
```

## 2. Start a source

A throwaway PostgreSQL with a few customers and their orders, on its own
Docker network so the containers Datadouble starts can reach it:

```
docker network create --subnet 172.31.0.0/24 shop
docker run --detach --name shop-db --network shop --ip 172.31.0.10 \
    --env POSTGRES_PASSWORD=local-only postgres:18.4-trixie
docker exec -i shop-db psql -U postgres -q < tests/nightly/shop.sql
```

## 3. Write a spec

Save this as `shop/shop.yml`. It copies the first five customers and their
orders, replaces names and emails, and stores the image in `/var/tmp/shop`:

```yaml
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_DSN}}
runtime: {docker: {network: shop}}
subset:
  seeds: {customer: "id <= 5"}
  follow: {down: [customer -> orders]}
anonymize:
  customer:
    name: {faker: name}
    email: {faker: email, unique: true}
store: [{filesystem: {path: /var/tmp/shop/store}}]
```

The connection names an environment variable, never a password itself.

## 4. Lock, plan and build

Datadouble runs as the engine's user (`999:999` for PostgreSQL), with the
Docker socket and every path it writes at the same path. Reading a source
needs the machine marked inside the production boundary (decision 98); this
one holds only test data, so mark it for these commands:

```
sudo install -d -o 999 -g 999 /var/tmp/shop /var/tmp/shop/store /var/tmp/shop/work
alias datadouble='docker run --rm --network host --user 999:999 \
    --group-add $(stat -c %g /var/run/docker.sock) \
    -v /var/run/docker.sock:/var/run/docker.sock -v /var/tmp/shop:/var/tmp/shop \
    -v "$PWD:$PWD" -w "$PWD" -e HOME=/tmp -e DATADOUBLE_BOUNDARY=inside \
    -e SHOP_DSN=postgresql://postgres:local-only@172.31.0.10:5432/postgres datadouble'
datadouble lock --spec shop/shop.yml --lock /var/tmp/shop/catalog.lock
datadouble plan --spec shop/shop.yml --lock /var/tmp/shop/catalog.lock
datadouble run plan,extract,load,transform,package,verify,store \
    --spec shop/shop.yml --lock /var/tmp/shop/catalog.lock --work /var/tmp/shop/work
```

The lock goes beside the spec by default; here it goes where Datadouble's
user can write it. `lock` records the catalog, so a new table or column later fails the plan
until someone decides what to do with it (strict mode). `run` prints each
stage, the verification result, and where it stored the image:
`/var/tmp/shop/store/shop/all/postgres-18/`.

## Next

- Promote, fetch and copies: the [CI/CD integration](../ci-cd.md) design.
- Jobs for your CI/CD system: [CI/CD libraries](ci-cd-libraries.md).
- A whole pipeline to copy: the [example repository](example-repository.md).
