# Quickstart

This page takes you from nothing to a verified, anonymized golden image of a
small PostgreSQL database, on your own machine. A **golden image** is what a
Datadouble build produces: a copy of the source's data, cut down to a subset,
with personal data replaced, checked, and packaged so that copies can be made
from it.

You will:

1. build Datadouble's container image;
2. start a throwaway PostgreSQL database to copy from;
3. write a **spec**, the file that tells Datadouble what to copy and how to
   anonymize it;
4. record the source's catalog, plan the build, and run it.

You need Docker and a checkout of
[datadouble/datadouble](https://github.com/datadouble/datadouble). Run every
command from the root of the checkout. The commands work on Linux, and on
macOS with OrbStack.

## 1. Build the image

Datadouble itself runs in a container. It doesn't bundle any database
software. Instead, it starts the database vendor's own images beside it
whenever it needs a server or a tool such as `pg_dump` (decision 72).

There is no published release yet, so build Datadouble's image from the
checkout:

```
docker build --target runtime --tag datadouble .
```

## 2. Start a source

Next you need a database to copy from, called the **source**. This one is a
throwaway PostgreSQL server holding a few customers and their orders.

It runs on its own Docker network, `shop`, at a fixed address. Later, the
containers Datadouble starts join the same network, which is how they reach
the source.

```
docker network create --subnet 172.31.0.0/24 shop
docker run --detach --name shop-db --network shop --ip 172.31.0.10 \
    --env POSTGRES_PASSWORD=local-only postgres:18.4-trixie
```

The server takes a few seconds to start. Wait until it accepts connections,
then load the sample data:

```
docker exec shop-db sh -c \
    'until pg_isready --quiet --host 127.0.0.1 --username postgres; do sleep 1; done'
docker exec -i shop-db psql -U postgres -q -v ON_ERROR_STOP=1 < tests/nightly/shop.sql
```

`shop.sql` creates two tables: `customer`, with 30 rows of names and email
addresses, and `orders`, with 90 orders that each belong to a customer.

## 3. Write a spec

A spec is a YAML or JSON file that describes one build: where the source is,
which rows to copy, which columns to anonymize, and where to store the image.
Save this one as `shop/shop.yml`:

```yaml
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
runtime:
  docker:
    network: shop
subset:
  seeds:
    customer: "id <= 5"
  follow:
    down: [customer -> orders]
anonymize:
  customer:
    name:
      faker: name
    email:
      faker: email
      unique: true
store:
  - filesystem:
      path: shop/out/store
      mode: "0644"
```

Here is what each section does:

| Section | What it says |
|---|---|
| `sources` | One PostgreSQL source, named `app`. Its connection string comes from the environment variable `SHOP_DSN`. |
| `runtime` | Start engine containers with Docker, on the `shop` network, so they can reach the source. |
| `subset` | Start from the first five customers (`id <= 5`), then follow references down from `customer` to their `orders`. |
| `anonymize` | Replace each customer's `name` and `email` with fake values. The fake emails are unique. |
| `store` | Keep the finished image under `shop/out/store`, readable by everyone. The default, `"0640"`, keeps it from other users, which matters for real data but not for this example. |

Notice that the connection names an environment variable. A spec never holds
a password itself, so it is safe to commit.

The [spec reference](spec-reference.md) documents all spec configuration options.

## 4. Lock, plan and build

A build takes three commands:

- `datadouble lock` reads the source's catalog (its tables, columns and other
  objects) and records it in a file called the **catalog lock**.
- `datadouble plan` reads the source's catalog again, checks the spec
  against it and the lock, and works out what a build would copy, without
  copying anything.
- `datadouble run` runs the build's stages: plan, extract, load, transform,
  package, verify and store.

Before you can run them, the Datadouble container needs a little setup.

### Prepare the output directory and the command

Datadouble runs as the user that the engine's copies are served as. For
PostgreSQL's official image, that user is `999:999`. The files Datadouble
writes then belong to the server that will open them.

Datadouble writes everything for this example under `shop/out`. Create that
directory, owned by user 999, so that Datadouble can write to it. A container
does this, so the command needs no `sudo` and is the same on every system:

```
docker run --rm --user 0 -v "$PWD:$PWD" --entrypoint install datadouble \
    -d -o 999 -g 999 "$PWD/shop/out"
```

On macOS, the directory still belongs to you, because Docker writes to your
Mac's files as you. Either way, you can read everything Datadouble writes there.

Datadouble's user also needs to start containers, so it needs the group that
owns the Docker socket. Docker runs inside a virtual machine on macOS, so ask a
container for that group rather than looking on your own machine:

```
DOCKER_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    --entrypoint stat datadouble -c %g /var/run/docker.sock)
```

Then define `datadouble` as an alias that runs the image:

```
alias datadouble='docker run --rm --network host --user 999:999 \
    --group-add "$DOCKER_GID" -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD:$PWD" -w "$PWD" -e HOME=/tmp -e DATADOUBLE_BOUNDARY=inside \
    -e SHOP_DSN=postgresql://postgres:local-only@172.31.0.10:5432/postgres datadouble'
```

Each option has a reason:

| Option | Why |
|---|---|
| `--network host` | The working servers Datadouble starts publish their ports on the host, and Datadouble must reach them there. |
| `--user 999:999` | Run as PostgreSQL's user, as described above. |
| `--group-add "$DOCKER_GID"` and the socket mount | Let that user start containers through the Docker daemon. |
| `-v "$PWD:$PWD" -w "$PWD"` | The checkout, at the same path as outside the container. The containers Datadouble starts mount directories under `shop/out` by their full path, so the path must be the same everywhere. |
| `-e HOME=/tmp` | User 999 has no home directory in the image. |
| `-e DATADOUBLE_BOUNDARY=inside` | Mark this machine as inside the production boundary, explained next. |
| `-e SHOP_DSN=...` | The connection string the spec refers to. |

The **production boundary** separates the machines that may read real
personal data from those that may not. Datadouble reads a source only on a
machine marked as inside it (decision 98). This machine holds nothing but
test data, so it is fine to mark it for these commands.

### Run the three commands

```
datadouble lock --spec shop/shop.yml --lock shop/out/catalog.lock
datadouble plan --spec shop/shop.yml --lock shop/out/catalog.lock
datadouble run plan,extract,load,transform,package,verify,store \
    --spec shop/shop.yml --lock shop/out/catalog.lock --work shop/out/work
```

By default the lock goes beside the spec, as `shop/catalog.lock`. Here it goes
in `shop/out`, because on Linux Datadouble's user can't write to `shop` itself.

`plan` doesn't write a file. It prints a summary of what a build would copy,
or fails if the source no longer matches the spec and the lock. To see the
full plan as JSON, put `--json` before the command:

```
datadouble --json plan --spec shop/shop.yml --lock shop/out/catalog.lock
```

`run` saves the same plan as `plan.json` in the build's work directory, under
`shop/out/work/shop/<generation>/plan/`.

When it finishes, `run` lists each stage it ran, then the verification
result, then the image summary it stored. Look at what it wrote:

```
cat shop/out/catalog.lock
ls shop/out/store/shop/all/postgres-18/
```

In that path, `shop` is the spec's name and `all` is the default image group,
which holds every table. `postgres-18` is the engine and the major version of
the source's server. The directory holds the image itself (a physical and a
logical archive), its summary, and the verification result.

### Why the lock matters

The lock is what keeps Datadouble safe as the source changes. Suppose someone
later adds a table or a column. The next plan fails, because the source no
longer matches the lock, and it keeps failing until someone decides what to
do with the new data. This is called **strict mode**.

To accept a change, run `lock` again. It lists what changed since the last
lock, so someone can check the new tables and columns for personal data. In
CI, the `lock` job that `datadouble ci init` writes does this. It runs only by
hand, and publishes the new lock and its changes for review.

## Clean up

To remove everything this page created:

```
docker rm --force shop-db
docker network rm shop
docker run --rm --user 0 -v "$PWD:$PWD" --entrypoint rm datadouble -rf "$PWD/shop"
```

The last command uses a container because on Linux the files under
`shop/out` belong to user 999, so you can read them but not delete them.

## Next

- Promote, fetch and copies: the [CI/CD integration](../ci-cd.md) design.
- Jobs for your CI/CD system: [CI/CD libraries](ci-cd-libraries.md).
- A whole pipeline to copy: the [example repository](example-repository.md).
