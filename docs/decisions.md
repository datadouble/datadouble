# Decision log and open questions

This page records the decisions that shape Datadouble, in the order they were
made. Look here when you want to know why Datadouble works the way it does, or
whether a question has already been settled. Open questions, when there are
any, are listed at the end.

## How to read this log {#how-to-read}

Each decision has a number and a section of its own. The section says two
things:

- **Decided:** what was decided.
- **Why:** the reason for it.

The numbers follow the order the decisions were made in, and they never
change. Other pages cite decisions by number ("decision 131") and link to them
as `decisions.md#decision-131`.

A later decision sometimes changes part of an earlier one. When that happens,
both decisions say so. The earlier one has a note under its title naming the
later decision. The later one ends with a line such as "Amends decision 54."
These notes use a few words with set meanings:

- **Amends:** changes part of the earlier decision. The rest still stands.
- **Refines:** makes the earlier decision more precise without reversing it.
- **Supersedes** (or **replaced by**): the later decision takes the place of
  the earlier one, or of the part the note names.

So to find the current rule on a subject, read the decision and then follow
its notes forward.

A decision stays settled unless a later decision changes it.

## Decided

### 1. Datadouble as the name {#decision-1}

**Decided:** The project is named **Datadouble**.

**Why:** The name describes the job: a realistic stand-in for real data. The
name is also free on PyPI, npm and GitHub.

### 2. Pluggable adapters for every step {#decision-2}

**Decided:** Every step's work that is specific to one system is done by a
pluggable adapter. The core holds the general logic, such as subset planning,
anonymization rules and verification checks. There are six kinds of adapter:

- engine adapters, which extract, load, transform, package and restore for
  one engine;
- storage adapters;
- replicate adapters;
- announce adapters;
- container runtime adapters;
- CI/CD adapters.

**Why:** Breadth of engines is a core goal.

### 3. Minimum set of supported systems {#decision-3}

**Decided:** Datadouble supports at least these systems:

- PostgreSQL
- MySQL
- SQL Server
- Oracle
- Redis
- MongoDB
- Salesforce

**Why:** This is the required coverage.

### 4. Subsetting planned up front {#decision-4}

**Decided:** Subsetting is planned up front, and the plan is pushed down into
extract.

**Why:** Sources can be terabytes.

### 5. One consistent snapshot for all reads {#decision-5}

*Amended by [decision 129](#decision-129).*

**Decided:** All reads in a run share one consistent snapshot, where the
engine allows it.

**Why:** It keeps the integrity of filtered tables.

### 6. JSON interface first, CLI as a thin layer {#decision-6}

**Decided:** The primary interface is JSON in and JSON out. The CLI is a thin
layer over it.

**Why:** The two interfaces cannot drift apart.

### 7. Resumable, chainable stages with manifests {#decision-7}

**Decided:** Stages write artifacts with manifests. Runs are resumable and
chainable.

**Why:** This is a requirement.

### 8. Jailer `.jm` support {#decision-8}

*Refined by [decision 81](#decision-81).*

**Decided:** Datadouble supports Jailer's `.jm` files natively. It also has a
converter, which fails on a schema mismatch.

**Why:** You design the subset in Jailer and run it repeatedly in Datadouble.

### 9. Excludes by glob {#decision-9}

*How globs match names is amended by [decision 128](#decision-128).*

**Decided:** Objects can be excluded by schema or table glob.

**Why:** This is a requirement.

### 10. Strict mode {#decision-10}

*Amended by [decision 130](#decision-130). Refined by
[decision 123](#decision-123).*

**Decided:**

- The `strict` setting takes `off`, `tables` or `columns`. The default is
  `columns`.
- Removed or changed objects always fail, whatever the setting.

**Why:** Unknown tables and columns are the usual way personal data (PII)
leaks.

### 11. Containers and sample data for every engine {#decision-11}

**Decided:** Every engine has containers and sample data. Salesforce uses
recorded interactions plus a fake org.

**Why:** Local development needs them.

### 12. One repository, separately published adapters {#decision-12}

**Decided:** All the code lives in one repository. Each adapter is published as
a separate package.

**Why:** The adapter interface will change heavily before 1.0.

### 13. Repository private during planning {#decision-13}

*Superseded by [decision 31](#decision-31).*

**Decided:** The repository stays private during planning.

**Why:** Publish when ready.

### 14. MIT licence {#decision-14}

**Decided:** The licence is MIT.

**Why:** Maximum ease of adoption.

### 15. Tier 1 CI/CD adapters {#decision-15}

**Decided:** The Tier 1 CI/CD adapters are GitHub Actions, GitLab CI, Jenkins
and Argo CD. Each one is tested end to end.

**Why:** Datadouble mostly runs inside other teams' pipelines.

### 16. Lifecycle split into jobs {#decision-16}

*The `announce` stage's name is set by [decision 44](#decision-44). The
`lock` job is added by [decision 123](#decision-123).*

**Decided:** The lifecycle is split into jobs, and the jobs use new stages and
operations:

- jobs: `build`, `promote`, `fetch`, `provision`, `release`, `prune`,
  `activate`;
- stages: `promote`, `fetch` and `announce`;
- operations: `release`, `prune` and `activate`.

**Why:** Jobs run in different environments, on different schedules, with
different credentials.

### 17. The single production crossing {#decision-17}

**Decided:**

- Everything up to `verify` runs where `extract` runs.
- `promote` into an export store is the only step that crosses out of
  production. It refuses unverified images.
- Non-production pulls from the export store only.

**Why:** Raw extracts and working databases contain real personal data.

### 18. CI/CD adapters stay thin {#decision-18}

*Refined by [decision 123](#decision-123).*

**Decided:** CI/CD adapters contain no pipeline logic. They never put data
artifacts in CI artifact storage.

**Why:** The first part has the same reason as the thin CLI
([decision 6](#decision-6)). The second is because CI storage is often outside
production.

### 19. No builds on vendor-hosted runners {#decision-19}

*Refined by [decision 98](#decision-98).*

**Decided:** `build` refuses to generate or run on vendor-hosted runners.

**Why:** This keeps production data from being pulled onto a vendor's
machines.

### 20. Verify after package, against a scratch restore {#decision-20}

**Decided:** `verify` runs after `package`, against a scratch restore of the
image.

**Why:** It verifies the artifact that ships, including that it restores.

### 21. Run outcomes {#decision-21}

**Decided:**

- A run ends in success, warning or failure.
- Anything that could leak personal data is a failure.
- There is no continue-on-error switch.

**Why:** Operators need a way to say "fix this soon" without weakening the rule
that Datadouble fails loudly.

### 22. Image identity, channels and summaries {#decision-22}

*Its "target engine version" is amended by [decision 120](#decision-120). The
image summary's name is set by [decision 45](#decision-45).*

**Decided:**

- An image is identified by spec, engine, target engine version and
  generation.
- Images are addressed through channels.
- Image summaries can be read without downloading the image.

**Why:** This gives cheap change detection, rollback, and test channels that
can't replace live images.

### 23. Pool and on-demand replication {#decision-23}

**Decided:** `replicate` supports two modes:

- pool mode, which keeps N unclaimed copies and recycles them by generation;
- on-demand mode.

A copy is either a volume or an instance.

**Why:** Large images copy more slowly than people will wait. Small ones suit
per-request copies.

### 24. Agent skills {#decision-24}

**Decided:** Datadouble has agent skills, in the Agent Skills format, for each
job, for the spec, and for a whole pipeline. They ship in this repository, both
as a Claude Code plugin and as plain skill directories.

**Why:** Agents increasingly do pipeline setup. Skills make that setup
repeatable.

### 25. Skills contain no logic {#decision-25}

*Refined by [decision 105](#decision-105).*

**Decided:**

- Skills contain no logic. All files come from `datadouble` commands.
- Skills never weaken safety settings.
- Skills never handle secrets.

**Why:** The no-logic rule has the same reason as the thin CLI
([decision 6](#decision-6)). The safety rule is because an agent must not trade
safety for a passing run.

### 26. This repository is the marketplace and the plugin {#decision-26}

**Decided:** This repository is both the Claude Code marketplace and the
plugin. There is no separate marketplace repository and no packaging step.

**Why:** Skills stay versioned with the CLI and cannot drift. An org-wide
marketplace can later reference this repository by tag.

### 27. Python as the implementation language {#decision-27}

**Decided:** Datadouble is written in Python.

**Why:** Python has mature drivers for every target engine, and the Faker
ecosystem. Because the interface is JSON, callers in other languages are
unaffected.

### 28. Strict static typing {#decision-28}

**Decided:**

- CI runs `mypy --strict` and `pyright` in strict mode.
- `ruff` runs with annotation rules.
- There are no stringly-typed values.

**Why:** Untyped dicts and magic strings make adapters hard to extend. Types
catch interface breaks before runtime.

### 29. Engine-native expressions and declared hook columns {#decision-29}

**Decided:**

- Anonymization strategies include engine-native expressions as column rules:
  SQL, MongoDB aggregation, Painless and Lua.
- `after_transform` hooks that rewrite personal data declare the columns they
  rewrite.

**Why:** An expression is often the simplest rule. Declaring the columns keeps
per-column hashes, reports and strict-mode resolution working.

### 30. Version 1 scope {#decision-30}

**Decided:** The scope of version 1 is [the v1 plan](v1-plan.md).

**Why:** Prove the core on one engine, then go wide in one long run.

### 31. Everything public {#decision-31}

**Decided:** Everything is public: this repository, the documentation site and
the example repository. The trademark and domain check is not a gate.

**Why:** Openness over caution, for an MIT project.

Supersedes [decision 13](#decision-13).

### 32. Copyright and licence on every file {#decision-32}

**Decided:** The copyright is "(c) 2026- Tight Line LLC", under the MIT
licence. Every source file carries a copyright line and
`SPDX-License-Identifier: MIT`.

**Why:** Ownership and licence are clear on every file.

### 33. Signed golden images {#decision-33}

**Decided:** `build` signs golden images with cosign. `promote` and `fetch`
verify the signature. [Decision 58](#decision-58) says how.

**Why:** Non-production can check that an image came from the production
pipeline.

### 34. Elasticsearch and OpenSearch {#decision-34}

*Refined by [decision 96](#decision-96).*

**Decided:** The supported engines add Elasticsearch and OpenSearch, as
separate adapters.

**Why:** Their APIs and clients have diverged since the fork.

### 35. Filesystem engine adapter {#decision-35}

*Refined by [decision 97](#decision-97).*

**Decided:** There is a filesystem engine adapter:

- `find` expressions select the files;
- shell commands do the cleanup;
- a files PII scan is mandatory.

**Why:** Rows often refer to file trees. Those files are needed alongside the
databases, and they may hold personal data.

### 36. Salesforce tests against the fake org {#decision-36}

**Decided:** Datadouble's tests run the Salesforce adapter against a fake org
and recorded cassettes. Cassettes are recorded separately, outside the test
run, against a development org.

**Why:** The test run never holds org credentials.

### 37. The changelog {#decision-37}

**Decided:**

- `CHANGELOG.md` uses the Keep a Changelog format, with an `[Unreleased]`
  section.
- Every user-visible change and every security fix gets an entry.
- The release script refuses to tag while `[Unreleased]` is empty.

**Why:** Users learn what an upgrade changes. Security-only releases are
possible, and explained.

### 38. Packages released together until 1.0 {#decision-38}

**Decided:** Until 1.0, all packages are versioned and released together, with
one changelog.

**Why:** There is one release gate and one changelog while the adapter
interface is still changing. Independent versions can follow once it is
stable.

### 39. YAML or JSON specs, one schema {#decision-39}

**Decided:** Specs are accepted as YAML or JSON. Both are validated against one
JSON Schema, generated from the typed models.

**Why:** YAML suits people editing by hand, and JSON suits programs. One schema
keeps the two identical.

### 40. Golden image formats {#decision-40}

**Decided:**

- The spec chooses the golden image formats, `physical` and `logical`, in
  `package.formats`. The CLI can override the choice.
- If the spec leaves it out, Datadouble builds every format the engine
  supports.
- An unsupported format fails the plan.

**Why:** Builds stay reproducible from the spec and manifest, and no engine
silently falls back to another format.

### 41. Several images from one build {#decision-41}

*Amended by [decision 129](#decision-129). Refined by
[decision 131](#decision-131).*

**Decided:** One build can produce several images, through named groups in
the spec:

- the extract is shared;
- each group gets its own working database;
- all images from a build share a generation;
- image identity includes the group.

**Why:** A build can make small images that are easy to reason about, plus
combined ones. They all come from one snapshot, without merging images later.

### 42. Jenkins shared library {#decision-42}

**Decided:** In v1, the Jenkins integration is a shared library. A Jenkins
plugin is a possible later addition.

**Why:** This avoids a Java plugin build and hosting process until users ask
for it.

### 43. Documentation site generated from the docs {#decision-43}

**Decided:** The documentation site is generated from `docs/*.md`. The Design
and Project sections are live from M0. The Using Datadouble section is added in
M10.

**Why:** The docs stay the single source of truth, and the public site shows
the design before there is software to document.

### 44. The `announce` stage name {#decision-44}

**Decided:** The stage that tells consumers where clones can be accessed, as
volumes or connection parameters, is named `announce`. Its adapters are announce
adapters.

**Why:** The stage never writes data. The name "publish" was rejected because
it read as writing database dumps into targets such as Secrets or Vault.

Amends [decision 16](#decision-16).

### 45. The image summary name {#decision-45}

**Decided:** The small file stored next to each image, which `fetch` reads
without downloading the image, is called the **image summary**.

**Why:** The name "sidecar manifest" was rejected because "sidecar" suggested a
Kubernetes sidecar container.

Amends [decision 22](#decision-22).

### 46. Zensical for the documentation site {#decision-46}

*Amended by [decision 136](#decision-136).*

**Decided:** The documentation site is built with Zensical, pinned to an exact
version.

**Why:** Material for MkDocs reached end of life on 5 November 2026. Zensical
is its successor from the same team. It is MIT-licensed, Python-based and
actively maintained, but still before 1.0.

### 47. `make`, `uv` and the root `pyproject.toml` {#decision-47}

**Decided:**

- Development tasks run through `make`.
- Python tooling runs through `uv`.
- Tests and lint configuration live in the root `pyproject.toml`.

**Why:** CI and people share one entry point.

### 48. Supported Python versions {#decision-48}

**Decided:**

- Python 3.13 is the minimum supported version and the type-checking target.
- CI runs the unit tests on every later release: 3.14 and 3.15 now, with new
  ones added as they come out.
- The container images run 3.15. Moving them to a new release is a deliberate
  change, not a Dependabot update.

**Why:**

- Installs from PyPI work on older CI runners and workstations.
- The images get the newest interpreter.
- The test matrix shows which dependencies lag a new release.

### 49. Working databases as throwaway servers {#decision-49}

*The binaries requirement is superseded by [decision 72](#decision-72). How
working servers are reached is set by [decision 74](#decision-74).*

**Decided:**

- Working databases are throwaway database servers. The engine adapter starts
  them in scratch space, one per image group.
- For PostgreSQL, a working database is a cluster of its own in scratch
  space.
- The machine that builds images needs the server binaries of the version the
  images are built for, and the source's extensions.

**Why:** Nothing shared holds personal data, and there is no server to
provision. A physical image is simply the stopped server's files.

### 50. Image archives and store layout {#decision-50}

**Decided:**

- Each format of an image is its own gzip tar archive. It holds the engine's
  files and the build manifest.
- For each generation, a store holds `<generation>.<format>.tar.gz` for each
  format, `<generation>.summary.json` and `<generation>.verify.json`.

**Why:**

- Consumers download only the format they use.
- gzip is in every supported Python.
- Verification runs after packaging, so its report sits beside the archives
  rather than in them.

### 51. Nested strategy and followed child edges {#decision-51}

**Decided:** The nested strategy (A) treats rows reached through a followed
child edge as already having their parent, when the reference targets a key.
True recursions run with the keys strategy (B). A true recursion is a cycle, a
self-reference, or children that pull in further parents.

**Why:** Otherwise every followed child edge would make strategy A cyclic. Both
strategies still choose the same rows.

### 52. What version 1 refuses to anonymize {#decision-52}

**Decided:** Version 1 refuses to:

- anonymize primary key or foreign key columns;
- `drop` on a virtual reference with `when`;
- `stub` or `drop` on a reference that leaves its image group (use `null` or
  `requires:` instead).

**Why:** Each of these needs integrity handling that cannot be verified yet.
Failing the plan is safer than guessing.

### 53. Runs, generations and the work directory {#decision-53}

**Decided:**

- A run belongs to a generation.
- The run's work directory (`--work`, default `.datadouble`) holds every
  stage's artifacts and manifests.
- `--resume GENERATION` continues the run, skipping stages whose input hashes
  match.

**Why:** Resume needs a stable anchor time and anonymization key. The work
directory is production scratch space and never leaves production.

### 54. No data values in messages {#decision-54}

*Amended by [decisions 121](#decision-121) and [139](#decision-139).*

**Decided:** Messages from engines and stores name objects, codes and states,
but never data values. PostgreSQL messages have quoted strings redacted. S3
errors give only the error code.

**Why:** Error messages reach CI logs, which are outside production.

### 55. The PII scan {#decision-55}

**Decided:**

- The PII scan flags emails, card numbers, US SSNs and formatted phone
  numbers.
- It does not flag reserved example domains and documentation ranges. The
  anonymizer uses these for every contact value it generates.
- Reports carry counts per column, never values.

**Why:** A report must not repeat the personal data it found. Generated values
must not fail their own verification.

### 56. S3 test service and sample datasets {#decision-56}

*Fixture storage is amended by [decision 78](#decision-78).*

**Decided:**

- The S3 test service is SeaweedFS, in the Compose default group.
- Sample datasets are not committed. They are fetched at image build, at a
  pinned commit and SHA-256. The dataset is Pagila v4.1.2, under the
  PostgreSQL License.

**Why:**

- MinIO no longer publishes community images.
- Every integration run covers the S3 adapter.
- The repository stays small while the data stays pinned.

### 57. Non-production settings in the same spec {#decision-57}

**Decided:** The non-production side is configured in the same spec as the
build, under `signing`, `trust`, `promote`, `fetch`, `replicate`, `announce`
and `prune`. Each adapter is given as a one-key mapping, like `store`.

**Why:** One file describes an image's whole life. The spec holds no secrets,
so non-production can read it.

### 58. How images are signed {#decision-58}

**Decided:**

- cosign signs the image's summary, without its signature field. The summary
  names every archive's SHA-256.
- The Sigstore bundle is stored as `<generation>.sigstore.json`.
- The spec gives a key as a secret reference (`{env: NAME}` or `{file: PATH}`), and
  cosign gets it as `env://NAME` or a path.
- Keyless signing works through the CI system's identity.
- The transparency log is on unless it is turned off.

**Why:** The signature covers the image's contents without signing gigabytes.
Datadouble never holds key material.

### 59. `fetch` skips staged generations {#decision-59}

**Decided:** `fetch` skips a generation that is already staged, whether it is
active or not.

**Why:** A rollback with `activate` then holds until a genuinely newer image is
promoted. The next scheduled fetch does not undo it.

### 60. Claiming pool copies {#decision-60}

**Decided:**

- Outside Kubernetes, consumers take a pool copy with
  `datadouble claim --owner`.
- Replicate adapters that can see claims themselves report them. For example,
  a bound PersistentVolume is claimed, and a released one is returned and
  removed.

**Why:** Pool mode needs a claim step everywhere. Kubernetes already has one.

### 61. The `k8s-pv` replicate adapter {#decision-61}

*Amended by [decision 124](#decision-124).*

**Decided:** `k8s-pv` is a replicate adapter. Each copy is a directory and a
PersistentVolume. The volume, with its labels, is also the announcement, so
there is no separate `k8s-pv` announce adapter.

**Why:** Creating the volume is what makes the copy usable on Kubernetes, and
the volume's phase is how claims are seen.

### 62. Instance copies {#decision-62}

*Amended by [decision 121](#decision-121).*

**Decided:**

- An instance copy runs the engine's server in a container, on a volume copy,
  as the engine adapter describes it (`ServerPlan`).
- Each copy gets a generated password. The password is passed through the
  environment, not a command line.
- Only `dotenv`, `k8s-secret` and `vault` receive credentials.

**Why:** Any engine can be served without the replicate adapter knowing about
it. Copy credentials stay out of job outputs and process lists.

### 63. btrfs copies {#decision-63}

**Decided:** btrfs copies are subvolumes filled with reflinked copies of the
staged image. They are not snapshots of a base subvolume.

**Why:** Copies are as cheap as snapshots, the adapter keeps no state, and
staging stays plain directories.

### 64. Test services and pinned tools {#decision-64}

*The Compose default group and Docker-in-Docker in CI are amended by
[decision 88](#decision-88).*

**Decided:**

- Docker-in-Docker and OpenBao join the Compose default group.
- k3s (`k8s`) and a loop-mounted btrfs (`btrfs`) are opt-in groups. They need
  privileged containers, and each has its own CI job.
- cosign, kubectl and the Docker CLI are pinned in the test image.
- Stand-in scripts let unit tests cover the code that drives those tools.

**Why:** Real tools prove the behavior. The stand-ins keep coverage complete on
any machine.

### 65. MongoDB tables and columns {#decision-65}

**Decided:**

- A MongoDB collection is a table, and a dotted field path is a column.
- Columns come from a sample of documents (`sample`, default 1000), merged with
  any `$jsonSchema` validator.
- Sampled fields are nullable, except `_id`.
- A field seen with several BSON types has kind `other`.

**Why:** MongoDB has no schema to read. A validator is the closest thing to
one, and sampling finds what the validator leaves out. So strict mode still
sees every field in use.

### 66. MongoDB snapshot reads {#decision-66}

**Decided:**

- MongoDB extract reads inside one snapshot session (`snapshot: true`). It
  needs a replica set or a sharded cluster.
- Collection and index listings run outside the session, because sessions do
  not support them.
- Once the session is open, extract lists the collections again and compares
  the catalog's fingerprint with the plan's.

**Why:** A snapshot session is MongoDB's consistent point-in-time read.
Checking the fingerprint catches a schema change between the listing and the
reads.

### 67. MongoDB query language in the spec {#decision-67}

**Decided:**

- For MongoDB, seeds, `when` clauses and `where` conditions are query filters
  in Extended JSON. `":as_of"` stands for the plan's anchor time.
- An `expression` rule is an aggregation expression.
- An assertion is `{"collection": ..., "pipeline": [...]}`. Its result
  documents are failures.

**Why:** Each piece is MongoDB's own language, as SQL is for the relational
engines. The anchor time keeps seeds stable across resumes.

### 68. No hooks or nested strategy on MongoDB {#decision-68}

**Decided:** Engines declare in their capabilities whether they run hooks.
MongoDB runs neither hooks nor the nested strategy (A). So MongoDB builds with
the keys strategy (B), and plans with hooks fail.

**Why:** MongoDB cannot evaluate another collection's selection inside a
query. A script language for hooks would be a second, unverified anonymizer.

### 69. MongoDB in the test image {#decision-69}

*Where MongoDB's server and tools run is set by
[decision 72](#decision-72).*

**Decided:**

- The test image holds no MongoDB software. The server and its tools run in
  the official `mongo` image.
- The Compose `mongodb` service runs that image, pinned by digest, as a
  one-member replica set. It is seeded with a generated cinema database.

**Why:**

- Snapshot reads need a replica set ([decision 66](#decision-66)).
- Generating the data keeps its licence clear.

### 70. Hash replacements the PII scan would flag {#decision-70}

**Decided:** If the PII scan would flag a hash replacement, the replacement is
retried with the next attempt. This can happen because a hex digest can hold a
Luhn-valid digit run.

**Why:** A hash must never fail its own image's verification.

### 71. SonarCloud quality gate blocks merges {#decision-71}

*(Where its coverage comes from is amended by [decision 88](#decision-88).)*

**Decided:** The required `sonar` job enforces the SonarCloud quality gate. The scan waits for the gate (`sonar.qualitygate.wait`) and fails the job when the gate fails.

Coverage roots are `src` directories. `scripts/check_coverage.py` fails the job if the coverage report names any analysed file more than once, or not at all.

**Why:** A red gate must block a merge. Dependabot pull requests run without the Sonar token, so requiring the SonarCloud app's own check would block them.

A report keyed by package-relative names (`__init__.py`) silently merged files and scored them as uncovered.

### 72. Engine software runs in vendor images {#decision-72}

*(Amended by [decision 129](#decision-129).)*

**Decided:** Engine software runs in the engine vendor's container images, never beside Datadouble. This covers working databases, restores for verification, and tools such as `pg_dump` and `mongorestore`.

- The default image is the vendor's official image of exactly the source's version (`postgres:<version>-trixie`, `mongo:<version>`). A dump from a newer minor release can hold what an older server rejects.
- A source's `image` (`sources.<name>.image`) overrides the default.
- The published `datadouble` image contains Datadouble, its adapters, the `docker` command, `kubectl` and cosign. It contains no engine software.

**Why:** Engine binaries tied Datadouble to one host's packages and versions. SQL Server and Oracle ship only as images in practice. The vendor's image matches the source's version and carries its own tools.

This supersedes [decision 49](#decision-49)'s requirement that the build machine hold server binaries. It also amends [decision 69](#decision-69): the test image carries no MongoDB server or tools.

### 73. Container runtimes are adapters {#decision-73}

*(Amended by [decisions 86](#decision-86), [124](#decision-124), [126](#decision-126) and [133](#decision-133).)*

**Decided:** Container runtimes are adapters (`datadouble.runtimes`). The spec's `runtime:` chooses one:

- `docker` drives any Docker-compatible daemon with the `docker` command.
- `kubernetes` runs pods with `kubectl`.

Containers see their paths at the same absolute path as Datadouble does. That is a bind mount, or the work claim mounted at the same path in every pod. Containers run as Datadouble's user and group.

**Why:** Builds must run on Kubernetes as well as on a host with Docker. Identical paths and one user let both sides share files without copying them or fixing ownership.

### 74. Working server credentials and network access {#decision-74}

*(Amended by [decision 121](#decision-121).)*

**Decided:**

- Working servers accept a password generated for each start. It is passed through the runtime's secret channel: the `docker` command's environment, or a Secret object made for the pod.
- Tools that need a working server run inside its container.
- Docker publishes server ports on `127.0.0.1` by default.
- Kubernetes connects to the pod's address, or through `kubectl port-forward` when Datadouble runs outside the cluster.
- Build-only credentials are removed before packaging.

**Why:** Working databases hold real personal data, so they are never open without authentication. Passwords never reach a command line or a pod spec.

Amends [decision 49](#decision-49): PostgreSQL's working clusters listen on the network, with a password, and have no Unix socket.

### 75. Cleaning up leftover containers {#decision-75}

**Decided:** Containers are labelled with the stage directory they serve. Before a stage runs again, the core removes labelled leftovers through the runtime.

**Why:** With servers in containers, cleaning up after an interruption no longer depends on the engine.

### 76. Milestone 4 in two pull requests {#decision-76}

*(Amended by [decision 80](#decision-80).)*

**Decided:** Milestone 4 is split into two pull requests:

1. The container runtimes, with PostgreSQL and MongoDB moved onto them.
2. MySQL, SQL Server and Oracle.

**Why:** Each is reviewable and green on its own, and the new engines are built only on the container model.

### 77. One place runs external programs {#decision-77}

**Decided:** External programs run only through `datadouble.programs`. It uses no shell and passes arguments as a list. It holds the repository's only subprocess suppressions: one for a program that runs to the end, and one for a program that keeps running.

The number of checker suppressions must equal a budget in `pyproject.toml`. Adding a suppression raises the budget in review, and removing one lowers it.

**Why:** Suppressions had grown with every adapter that wraps a tool, mostly the same subprocess rule repeated. A budget makes each new one a visible decision, and otherwise lets the count only go down.

### 78. Constant Sakila test fixtures {#decision-78}

**Decided:** Test fixtures are committed and constant.

- Sakila is the sample data for MySQL, SQL Server and Oracle. It comes from jOOQ's collection (commit `e089a5b1`), which has it for every relational engine.
- `scripts/sakila_bulk.py` converted its rows once into per-table bulk-load files in `dev/sakila/`, with every contributor's BSD notice.
- A Compose `fixtures` service puts those files in a volume. The database containers mount it read-only and bulk-load it at start.
- Each engine's schema comes from the same collection at that commit.

**Why:** Tests need data that never changes. One dataset across the relational engines makes their results comparable, and it matches Jailer's Demo-Sakila model.

Sakila's insert scripts took 13 minutes to load, where the bulk files take seconds. Keeping the data out of images keeps it out of everything that is built and published.

Amends [decision 56](#decision-56).

### 79. The MySQL adapter {#decision-79}

*(The catalog is amended by [decision 128](#decision-128).)*

**Decided:** The MySQL adapter's catalog is the URL's database.

- **Extract.** Keys and rows are read in one `START TRANSACTION WITH CONSISTENT SNAPSHOT` connection. Rows are read unconverted, in `LOAD DATA`'s text format. `mysqldump` writes the schema and, separately, the triggers, with `DEFINER` clauses removed. The catalog fingerprint is checked inside the snapshot.
- **Load.** Load runs the schema, then `LOAD DATA` with foreign-key checks off, then the fallbacks, then the triggers.
- **Transform.** Transform sets triggers aside and keeps `ON UPDATE` columns, since MySQL can switch neither off per session.
- **Users.** Tools in a working server's container connect as `root` over its socket. A build user with a per-start password serves Datadouble, and it is dropped before the server stops.
- **SQL.** One helper composes all SQL from quoted identifiers, placeholders and spec SQL.

**Why:** MySQL has no exported snapshots, no versioned catalog and no session switch for triggers, so each is handled where it comes up. The vendor's own text format round-trips every type without conversion in Python.

### 80. Milestone 4 in five pull requests {#decision-80}

**Decided:** Milestone 4 is five pull requests:

1. Container runtimes (done).
2. MySQL.
3. The Jailer front end, with its Sakila key-set comparison.
4. SQL Server.
5. Oracle.

**Why:** Each engine and the Jailer front end are large enough to review on their own, and each stays green.

Amends [decision 76](#decision-76).

### 81. Jailer subsets {#decision-81}

**Decided:** A Jailer subset is a spec's `subset.jailer` (model and data model). It is translated against the live catalog at plan time. Alternatively, `import-jailer` imports it once as native settings.

The translation works like this:

- Unrestricted child directions become followed edges.
- `ignore` on a parent direction becomes `on_missing: null`.
- Equality associations become declared or virtual references.

In version 1, these features fail the plan by name: restriction conditions, non-equality joins, subject limits, parameters and column filters.

**Why:** Running a different subset from the one designed would be worse than refusing. The translated features cover the Sakila model the milestone compares, and each missing feature needs a native equivalent first.

Refines [decision 8](#decision-8).

### 82. Timeouts for CI jobs and tests {#decision-82}

**Decided:** Every CI job sets `timeout-minutes`, a few times its normal run. `scripts/check_workflows.py` enforces this in `make lint-workflows`. Every test fails after 10 minutes and shows its stack (`pytest-timeout`).

**Why:** A runner that stopped responding held a pull request's checks pending for over two hours under GitHub's six-hour default, with no log to show why.

### 83. SQL Server images, user and licence {#decision-83}

*(Amended by [decisions 86](#decision-86) and [107](#decision-107).)*

**Decided:** SQL Server working databases run in Microsoft's `mcr.microsoft.com/mssql/server` image.

- **Image.** The image matches the source's release and cumulative update, for example `2022-CU28-ubuntu-22.04`, read from `SERVERPROPERTY`. The build must match the source's build. A source on a security update (GDR) names its image in `sources.<name>.image`.
- **User.** SQL Server in that image runs only as its own uid and gid, 10001. So Datadouble must run as 10001:10001 (its image and the test runner do), and the adapter refuses otherwise.
- **`server`.** A source's `server` (`sources.<name>.server`) is a generic setting for working servers, and each engine adapter checks it. For SQL Server it carries `accept_eula: true`, without which the adapter refuses, and the edition (`Developer` by default). Other engines define their own `server` settings ([decision 107](#decision-107)).

**Why:** Microsoft tags images by update, not by build. The server will not start as another user, and running Datadouble as that user is simpler than changing ownership both ways. Accepting a licence is the operator's act, never Datadouble's.

### 84. SQL Server extract and load {#decision-84}

**Decided:**

- The SQL Server source must allow snapshot isolation.
- Extract reads keys, the schema and rows in one `SNAPSHOT` transaction, and runs nothing in a container.
- The server converts every value to text in a form it reads back exactly: hexadecimal binary, ISO 8601 times to 100 ns, and floats to 17 digits.
- Values are written as length-prefixed UTF-16 fields, with -1 for NULL. `BULK INSERT` loads them with a format file.
- Tables with types or features the adapter cannot copy exactly are refused by name until excluded. These are `sql_variant`, CLR and alias types, FILESTREAM, memory-optimized, temporal, partitioned and graph tables, and columnstore, XML, spatial or full-text indexes.

**Why:** Snapshot isolation is SQL Server's consistent read without locks. SQL Server's CSV reader cannot tell empty strings from NULL, and its Linux build accepts no code page. Length-prefixed Unicode text round-trips every supported type.

### 85. SQL Server schema scripting and images {#decision-85}

**Decided:** The SQL Server adapter scripts the schema from the catalog views, in phases:

1. Schemas and sequences.
2. Tables with keys, defaults and identity.
3. Indexes and checks, foreign keys, modules in dependency order, and triggers.

Each statement records the table and objects it needs. So a group's database leaves out whatever refers to tables the group lacks.

Modules keep their `ANSI_NULLS` and `QUOTED_IDENTIFIER` settings, and they are created as a user whose default schema is theirs.

Each working-database start is a new instance, whose system databases sit in a private directory. The database's files are attached and detached. So:

- a physical image is the database's data and log files, and carries no logins or server settings;
- a logical image is the schema and rows in extract's format.

**Why:** Microsoft publishes no image with a schema tool (`sqlpackage`), and [decision 72](#decision-72) keeps engine software out of Datadouble's image. Scripting the catalog is what such tools do. Refusing what is not scripted keeps a copy from silently differing.

### 86. Each engine's image owner {#decision-86}

*(Amended by [decision 121](#decision-121).)*

**Decided:** Each engine adapter declares `owner`: the `uid:gid` that serves a copy of its images in its vendor's image.

| Engine | `owner` |
|---|---|
| Official PostgreSQL, MySQL, MongoDB and Redis images | 999:999 |
| Elasticsearch | 1000:0 |
| OpenSearch | 1000:1000 |
| SQL Server | 10001:10001 |
| Oracle | 54321:54321 |

Datadouble runs as exactly that user and group to build, fetch and replicate the engine's images, and refuses otherwise. So a physical image's files belong from the start to the server that will open them, and no copy needs its ownership changed.

Datadouble's image works under any uid. The integration suite runs each engine's tests as its owner and combines their coverage.

**Why:** The engine and Datadouble share every file they hand each other, and the engines create their files readable only by their owner. SQL Server and Oracle run only as their own users. A `btrfs` or `k8s-pv` copy keeps the owner it was built with, so a physical image built as anyone else could not be served.

Amends [decisions 73](#decision-73) and [83](#decision-83).

### 87. The Oracle adapter {#decision-87}

**Decided:**

- **Catalog.** The catalog is the connection's `schemas`, by default the connecting user's own.
- **Extract.** `DBMS_METADATA` scripts the schemas just before a `READ ONLY` transaction reads keys and rows. The catalog is checked inside that transaction. The server converts values to text it reads back exactly.
- **Working databases.** Each working database is a pluggable database (PDB) whose datafiles live in the stage directory. For each stage it is plugged into a throwaway Oracle Database Free container, and unplugged before the container stops.
- **Images.** A physical image is the PDB's datafiles and manifest. A logical image is the scripted schema and the rows.
- **Server image.** Working databases and the test source use the full Oracle Database Free image of exactly the source's version. The `-lite` image decompresses its pre-built database on every start (78 s against 38 s), and it leaves out XML DB, which `DBMS_METADATA` needs.
- **Users.** Each schema becomes a user without a password in the copy. Grants and other users are not copied.

**Why:** `DBMS_METADATA` is Oracle's own schema tool and needs no server-side files, while Data Pump writes on the source's server. A read-only transaction is consistent without flashback privileges.

Plugging moves only the PDB, a few hundred megabytes, where the container database is 4 GB. A start costs about 35 s of Oracle's own startup.

### 88. Integration tests run in groups {#decision-88}

*(Amended by [decision 89](#decision-89).)*

**Decided:** The integration suite runs in groups, listed in `tests/integration-groups.txt`. Each group has its own user, Compose services and test paths, and each is its own CI job, run in parallel.

- `make test-group` runs one group, and `make test-integration` runs all of them.
- In CI, the tests drive the runner's own Docker daemon (`compose.host.yaml`). Developers' machines keep Docker-in-Docker by default.
- Each group keeps its coverage data. A job with the required name `Integration coverage` combines it and applies the 100% gate.
- `sonar` scans that combined report instead of running the suite again.
- The test image is built with GitHub's build cache. Engine images are pulled from their registries.

**Why:** One job ran every engine's tests in sequence and Sonar ran them all again, so a pull request took 27 minutes, and that grew with every engine. A throwaway runner needs no isolation from itself, and one daemon holds one copy of each image. Engine images are too large for the cache to beat the registry.

Amends [decisions 64](#decision-64) and [71](#decision-71).

### 89. Sharding slow integration groups {#decision-89}

*(Amended by [decision 90](#decision-90).)*

**Decided:** A slow integration group is split into shards, and each shard is its own CI job. `tests/integration-groups.txt` sets how many shards a group has, and CI's job matrix is read from that table.

- Shard K of N runs every Nth test in collection order, starting with the Kth (`pytest --shard K/N`, `scripts/shard.py`).
- pytest keeps only failed tests' temporary directories.
- `DOCKER=host` runs keep theirs in `/var/tmp/datadouble-work`, which `make clean-work` and the next run empty.
- Locally, groups run whole. Before a push, only the groups of the engines that changed need running.

**Why:** Sharding divides a slow group's time without changing what the tests run or how Datadouble starts engines, which reusing engine containers would change. Each shard pays the job's two minutes of setup again, so only groups whose tests take several minutes get more than one shard. Interleaving spreads a file's slow tests across shards, with no recorded timings to maintain.

Working databases' files filled a 12 GB `/tmp` tmpfs in one Oracle run, and they would fill a CI runner's disk.

Amends [decision 88](#decision-88).

### 90. Balancing shards by recorded durations {#decision-90}

**Decided:** Shards are balanced by the seconds each test took in CI, recorded in `tests/integration-durations.json`. Tests are placed longest first, each to the shard with the least time so far. Unlisted tests count as the average.

Every integration job records its tests' durations, and the coverage job merges them into an artifact. Someone commits that artifact by hand when the shards drift apart.

**Why:** Round-robin put Oracle's longest tests (whole builds, the two-strategy comparison) in the same two of six shards. Those took 26 minutes, against 8 to 13 for the rest. Durations change only when tests do, so a committed file is enough. A stale one only unbalances the shards. It never skips or repeats a test.

Amends [decision 89](#decision-89).

### 91. Working databases keep default sizes {#decision-91}

**Decided:** Working databases keep each engine's default sizes. So physical images carry the engine's fixed overhead:

- 73 to 211 MB unpacked for PostgreSQL, SQL Server, MySQL and MongoDB;
- 873 MB for Oracle's pluggable database.

`TESTING.md` records these sizes from every CI run's `image-sizes` artifact. Oracle's is the case to revisit if copies of real Oracle images prove costly.

**Why:** Measured in M4.5, stored images compress the empty space to a few MB, except Oracle's (88 MB). The overhead is small beside a real subset's size. Shrinking engine files (redo logs, undo, temporary tablespaces) would trade away settings that consumers' servers expect, for no observed problem.

### 92. M5 in three pull requests {#decision-92}

**Decided:** M5 is three pull requests: Redis, then Elasticsearch, then OpenSearch.

**Why:** One engine per pull request kept M4's reviews and CI runs small ([decision 80](#decision-80)).

### 93. The Redis adapter {#decision-93}

**Decided:**

- **Tables.** The Redis adapter's tables are key patterns in each database (`db0`, `db1`…). ID-like key segments are written `{}` (`user:{}:sessions`), and patterns can be declared for the rest.
- **Columns.** `_key` holds the values the placeholders stand for, and it is the key. The other columns are what the keys hold: `value`, hash and stream fields, `members`, `scores`.
- **Extract.** Extract reads only the selected keys: `SCAN` by pattern, then `TYPE` and `DUMP`. So each key is as it was when read. There is no point-in-time snapshot across keys, and the manifest records how long the extract took.
- **Subsets.** Seeds and `when` clauses are globs over `_key`. Datadouble evaluates conditions itself. Only strategy B is available.
- **Images.** A physical image is the RDB file the working server saves. There is no logical format. Copies keep no expiry.
- **Catalog rules.** A segment is literal only if it is name-shaped and not ID-like. The catalog fails, naming no values, when a prefix has more than 64 distinct first segments, or 16 at a later position. A declared pattern is a table even before any key matches it.

**Why:** Redis has no transactions to read under, and no query language. Streaming a whole RDB through replication would be point-in-time, but it copies the whole dataset first, which the subset-at-the-source principle rules out.

`{}` reads as a placeholder and means nothing to the glob matching that excludes use, where `*` would. Key-encoded references (the case [decision 3](#decision-3)'s requirements name) become ordinary declared references.

### 94. The Elasticsearch adapter {#decision-94}

**Decided:**

- **Tables.** The adapter's tables are the open indices that the connection's `indices` patterns match, keyed by `_id`. Columns are the field paths their mappings declare. Objects and nested fields are flattened. Multi-fields and aliases are left out.
- **Undeclared fields.** Indices whose mappings allow undeclared fields (`dynamic: false` or `runtime`) are sampled for more. The extract fails on a copied document that holds a field the catalog does not list.
- **Consistency.** Every read goes through one point in time over all of the source's indices.
- **Subsets.** Seeds and `when` are Query DSL. Value sets become `ids` and `terms` queries, so references must be `keyword`, numeric or `_id` fields. Only strategy B is available.
- **Index copies.** Copies keep mappings, aliases and the settings that describe the index. They drop allocation, lifecycle, ingest pipelines, write blocks and replicas.
- **Images.** A physical image is a snapshot repository, restored into a copy's data directory on its first start. A logical image is the same NDJSON dump the extract writes.
- **Rules.** Expression rules are Painless scripts run by `_update_by_query`.
- **Refused.** Data streams, and indices without a whole `_source`, are refused.

**Why:**

- Mappings are Elasticsearch's own schema. But `dynamic: false` lets `_source` carry fields no mapping lists, so only checking what the extract copies makes strict mode complete.
- A point in time is the one consistent view across indices.
- A snapshot repository is the format any cluster of the same or a later version restores. A copied data directory is unsupported.
- A `terms` query on a `text` field matches tokens, not values, so it would silently drop rows.

### 95. Elasticsearch client and working nodes {#decision-95}

*(The per-start password is amended by [decision 96](#decision-96).)*

**Decided:**

- The Elasticsearch adapter speaks the REST API through Elastic's `elastic-transport`, not the official client.
- Its errors name the server's error type and HTTP status, never the reason.
- Working nodes run with security on, over plain HTTP. The `elastic` user's password is generated for each start, as the container's bootstrap password.
- A shell around the node reports its normal stop (exit status 143) as success.

**Why:** Each major version of the official client talks only to servers of its own major version, and sources run 7.17, 8 and 9. A reason can quote the document that caused it. The bootstrap password lives in the container's keystore, not the data directory, so no image carries it.

### 96. The OpenSearch adapter {#decision-96}

**Decided:** The OpenSearch adapter is the Elasticsearch adapter's package with OpenSearch's product description. That description covers:

- its image;
- its security plugin: the demonstration configuration over plain HTTP, with the `admin` user's password set at a node's first start;
- its point-in-time API (`_search/point_in_time`).

Everything else, from the catalog to images and rules, is the same code. Further:

- Working nodes of both products keep one password per working database, beside its data directory.
- Sources are OpenSearch 2.4 or later.
- For both products, indices whose names start with a dot are never tables.

**Why:** Decision 34 keeps the adapters separate because the products' APIs diverged. The part this adapter uses (mappings, searches, bulk, snapshots, Painless) did not diverge, and two copies of it would drift.

OpenSearch keeps its users in the data directory, so a node cannot take a new password at each start. Its image refuses passwords without capitals and symbols, which the replicate adapters do not make, and the installer's test mode accepts them. OpenSearch's security index is an ordinary dot-named index.

Refines [decision 34](#decision-34), and amends [decision 95](#decision-95).

### 97. Files as a second source {#decision-97}

**Decided:** A spec's `files` section is a second source. The core builds it in the same generation, through the `files` engine adapter.

- **Tables.** Each top-level directory of `connection.root` is a table. Its rows are the directory's regular files, keyed by `path` from the root, with `size` and `content` columns.
- **`select`.** It gives each top-level directory `find` tests, run with GNU find: `-mtime -30`, for example, or `-false` to select none of its own files. Tests that act or follow links are refused.
- **`paths_from`.** It adds files by path, from values in the chosen rows of a table. It gives a table and either:
    - a template over its columns (`documents/{customer_id}/{file_name}`), optionally with a regular expression whose first group is the path (for URLs and prefixed keys); or
    - `table.column`, for a column that holds the path.

    The values are read inside the source's snapshot, through strategy B's conditions, which stand alone. Paths that leave the tree or are missing are skipped. Values the pattern does not match are counted in a warning.
- **Coverage.** Every top-level directory must be selected or excluded (the planner's `complete` capability). Strict mode is otherwise off for files.
- **`cleanup`.** Its commands are `after_transform` hooks. They run with `sh` in the copy's root, in a container (`files.image`, Debian by default).
- **Images.** Images are the tree (physical), one per group, sharing the spec's groups and stores.
- **PII scan.** The scan reads `path` and the printable text runs in every file, including binary files' metadata.
- **Consistency.** Version 1 takes no filesystem snapshot, so extract runs only with `accept_inconsistent: true`.

**Why:** The plan's single list of `find` commands would make the core parse `find` to learn which directory each one covers. Tests per directory make every directory a seed like any table's, and leave `find` to the adapter.

Rows rarely hold the bare path. They hold URLs, keys with prefixes, or the pieces of a path in separate columns. A template with a pattern covers these in every engine, where an expression in the source's language would need support in each adapter. Reading `paths_from` through the source's snapshot needs no new interface, since `project` already reads columns' values for chosen rows.

Text runs find EXIF and similar metadata, which the plan's example cleans with exiftool, without format-specific parsers.

Refines [decision 35](#decision-35).

### 98. The production boundary mark {#decision-98}

*(Refined by [decision 123](#decision-123).)*

**Decided:** Some commands run only on machines marked as inside the production boundary, by `DATADOUBLE_BOUNDARY=inside` in their environment. These are:

- commands that read a spec's source: `lock`, `plan`, `import-jailer`, and `run` whenever it plans or extracts;
- `promote`.

On any other machine they fail with `outside_boundary` before connecting.

- The operator sets the mark where each runner is configured.
- Development and test environments mark themselves, since they read only their own fixtures.
- The CI/CD adapters' check for vendor-hosted runners stays as a second guard.

**Why:** Detecting vendor-hosted runners only catches the vendors an adapter knows, and the variables they happen to set. Requiring a positive mark refuses every machine nobody vouched for, including unfamiliar CI systems, at the cost of one setting per runner.

Refines [decision 19](#decision-19).

### 99. M8 in five pull requests {#decision-99}

**Decided:** M8 is five pull requests:

1. GitHub Actions, with the shared CI/CD core and conformance suite.
2. GitLab CI.
3. Jenkins.
4. Argo CD.
5. The nightly runs on hosted services.

The first four are the Tier 1 systems, one per pull request. The nightly hosted GitLab run waits until a GitLab.com project exists, as Salesforce's cassettes wait for an org ([decision 36](#decision-36)). M7 waits for that org, and comes after M8 to M10.

**Why:** Each system is large enough to review on its own, as engines were in M4 and M5 ([decisions 80](#decision-80) and [92](#decision-92)).

### 100. How `ci init` writes jobs {#decision-100}

*(Refined by [decisions 123](#decision-123) and [140](#decision-140). Amended by [decisions 126](#decision-126) and [127](#decision-127).)*

**Decided:** `ci init` (`ci.init`) writes one side of the boundary per call.

**The core decides; adapters only write.** The core decides every job's command lines, triggers, parameters, secrets and host paths. Triggers are UTC schedules, `promote` after `build`, a pool's `provision` after `fetch`, and pull or merge requests for on-demand copies. Adapters only write that in their system's form.

**Each job gets only what it needs.** A job gets the secrets and absolute paths its own spec sections name. It also gets `COSIGN_PASSWORD` for a key-signed build, and any `--env NAME`.

**Native steps run the pinned image.** They run the pinned Datadouble image with `docker run`:

- as the engine's user;
- on the host's network;
- with the runner's Docker socket, and those paths at the same path.

**Retries resume.** `build` passes the CI run's ID as `--retry-key`. A run with a key resumes the generation that an earlier attempt with that key started.

**Requests that name their system.** A request can name its system (`--ci`, or `ci` in JSON). Then its resolved secrets are masked, and its response is reported as native outputs, summary and state. Every installed CI/CD adapter is asked whether the machine is its vendor's hosted runner.

**GitHub Actions.** One action (`ci/github-actions`) serves every job. A warning is a passing step with a warning annotation.

**Why:** Jobs stay identical across systems and cannot drift from the CLI. The image is the one signed artifact, and running it lets jobs run as the engine's user ([decision 86](#decision-86)) on any runner with Docker.

A job holding only its own credentials keeps production secrets out of non-production jobs, and out of jobs that don't need them. A retried CI run gets the same ID, so retries resume without anyone naming a generation.

### 101. On-demand copies and GitLab jobs {#decision-101}

**Decided:**

- On-demand `replicate` makes copies up to the spec's `size` per owner. So asking again (a retried job, or a merge request's pipeline on every push) makes none.
- GitLab jobs include their job template from this repository at the release's tag, pinned by SHA-256 (`include: remote` with `integrity`). This lasts until a GitLab mirror can publish CI/CD Catalog components.
- A GitLab job runs when the pipeline's `DATADOUBLE_JOB` names it, since schedules live outside GitLab's configuration.
- `ci init` returns the setup a system's configuration cannot hold (`instructions`).
- On-demand copies belong to a merge request's environment, whose `on_stop` job releases them.

**Why:** Copies per request must not pile up when a system repeats a request. Remote includes with integrity give a versioned, verified step without a GitLab project, and GitLab 19.5 checks the hash. Schedules and merge request pipelines must be set up in GitLab itself, so the person running `ci init` is told what to do.

### 102. Jenkins jobs {#decision-102}

*(Amended by [decision 127](#decision-127).)*

**Decided:**

- Each Jenkins job is a declarative Jenkinsfile. It loads the shared library (`ci/jenkins`) from this repository at the release's tag, with `library` and `libraryPath`, so no global library needs setting up. The library has one `datadouble` step.
- Jenkins has no request events that every installation shares. So on-demand `provision` and `release` take an `OWNER` parameter, for a request's own pipeline to pass.
- A retried Jenkins build is a new build. So `build` takes a `RETRY_KEY` parameter naming the failed build's `BUILD_TAG`.
- `promote` follows `build` through an `upstream` trigger that accepts an unstable build.
- The conformance suite tests the shared library, rather than JenkinsPipelineUnit.

**Why:** Loading the library by tag pins it the way the GitHub action and GitLab template are pinned. Parameters are the one request and retry mechanism that every Jenkins has.

An unstable build is a warning: its image is built and verified, so it must still be promoted. The suite runs the step in real Jenkins, which a unit-test harness for Groovy would only imitate.

### 103. Argo CD on Kubernetes {#decision-103}

*(Amended by [decision 133](#decision-133).)*

**Decided:** On Kubernetes, `ci init --system argocd` writes an Argo CD Application per side. The Application installs this repository's Helm chart (`ci/kubernetes/chart`) at the release's tag, with the jobs, the spec and its catalog lock as values. `--runner` sets the namespace.

- **Pods.** Pods run the Datadouble image itself, as the engine's user. Every path in the spec must share one PersistentVolumeClaim, mounted at the same path.
- **Jobs.** Each job is a CronJob, suspended when it has no schedule. A following job also runs as a later step of its leader's pod.
- **Boundary mark.** The mark is a `datadouble-boundary` ConfigMap that the operator creates in production namespaces.
- **On-demand copies.** These are Argo CD hooks on an Application that sets `owner`, one per request, from an ApplicationSet. That Application renders only those hooks, which use its side's service account and spec. PreSync hooks run before an Application's own resources exist, and PostDelete hooks run after they are gone.
- **Outcomes.** A Job has no warning state, so a warning succeeds and says so. Outputs are the termination message.

**Why:** Argo CD renders the chart from this repository, so the spec travels in the values. CronJobs cannot follow one another, so a leader's pod runs its follower. A ConfigMap that the generated configuration never creates keeps the mark the operator's, as [decision 98](#decision-98) requires. Hooks are Argo CD's way to act when an Application comes and goes.

### 104. The nightly hosted run on GitHub {#decision-104}

**Decided:**

- The nightly hosted run on GitHub runs committed workflows that `ci init` writes for `tests/nightly/nightly.yml`. `scripts/nightly.py` writes them, and `make lint-workflows` checks them.
- The workflows use the checkout's action and have no schedules.
- They run on a self-hosted runner. A GitHub-hosted job starts that runner on its own machine for the night, and removes it afterwards.
- The job runs the workflows in order, follows the `workflow_run` chains, and checks that its own hosted machine is refused.
- The build signs keyless, through GitHub's identity token.

**Why:** GitHub runs only workflows in the repository, and a job's own token cannot push workflow files. So they are committed and kept current by a check. A runner on the hosted machine needs no infrastructure of ours. Starting each workflow from the job means none waits for a runner that is not there.

### 105. Skills rules and skill evals {#decision-105}

**Decided:**

- Skills follow one shared rule file, `skills/RULES.md`.
- CI/CD configuration comes only from `ci init`.
- Spec content has no generator. So an agent may change a spec for a person, and checks the change with `datadouble plan`.
- The skill scenarios live in `evals/` as `claude plugin eval` cases. They use deterministic graders where possible (the command used, files written or not, secret values absent), and judged ones for explanations.
- A maintainer runs them with `make test-skills`, against a copy of the plugin's files, before a release and after changing skills.
- They are outside the standard CI pipeline, which runs only `claude plugin validate --strict`.

**Why:** No command writes a spec, and refusing every spec edit would leave the spec skill nothing to do. Deterministic graders keep the safety cases (hosted runners, strict mode, secrets) from depending on a judge. Each scenario runs Claude several times, which costs a few dollars a pass. That is too much for every pull request.

Refines the "never by hand" rule of [decision 25](#decision-25).

### 106. Generated references and the example repository {#decision-106}

**Decided:**

- The documentation's references are generated and checked by `scripts/reference.py`. The spec reference comes from the JSON Schema, whose fields carry their docstrings. The CLI reference comes from the parser. `make lint` fails when either is stale.
- The example repository, datadouble/example-pipeline, holds a spec, its lock, and the workflows `ci init` wrote for both sides. The workflows are disabled on GitHub, since the repository has no runners.
- `make demo` runs them all with act on one machine. It builds the Datadouble image from a checkout, and tags it as the release the workflows pin until a release exists.
- The example repository's own CI runs the demo on every push and nightly.

**Why:** Generated pages cannot drift from the code. The example shows exactly what a team commits, unchanged, and still runs green from a fresh clone without anyone's runners.

### 107. Source add-ons are requirements {#decision-107}

**Decided:** A source's add-ons are requirements.

- Each engine adapter reports, in the catalog, the extensions, plugins, modules or packages a source uses, with versions.
- The lock records them, and strict mode treats them like tables.
- Before it opens the snapshot, the build checks that the working image provides each one, at the source's version. Otherwise it fails with `requirement_unmet`, listing every gap.
- A source's `omit` (`sources.<name>.omit`) leaves named add-ons out. Anything that uses them must be excluded.
- A source's `server` takes the settings add-ons need, such as PostgreSQL's `shared_preload_libraries`.
- Requirements are part of a catalog's hash only when the catalog has some, so a lock of a catalog without requirements stays valid.

PostgreSQL ships first. Redis, Elasticsearch, OpenSearch and MySQL follow, then SQL Server, Oracle and MongoDB. Salesforce's managed packages are part of M7.

**Why:** A missing extension failed mid-build in a raw restore error, after the snapshot had been read. A change in a source's add-ons went unnoticed. And copies could run other versions than the source. Every engine has the same problem in its own terms, so the shape is shared and the details stay in adapters.

Amends [decision 83](#decision-83).

### 108. Requirements for MySQL, Redis and search engines {#decision-108}

**Decided:** This is the second set of requirements per engine.

- **MySQL** reports only storage engine and full-text parser plugins, by library. `load` installs them in the working server, whose data directory keeps them.
- **Redis** reports modules from `MODULE LIST`. Every server Datadouble starts loads every module in the image's module directory. A key of a module's type gets a `value` column of kind `other`.
- **Elasticsearch and OpenSearch** report `_nodes/plugins`, and list the image's plugins with the plugin tool, so the check starts no node.

**Why:** Authentication, password-policy and audit plugins change the server's behaviour, not what it can load, and they would break the working server's own build user. Redis working servers skipped the image's entrypoint, and so its modules. Once modules load, a module's values must not reach a copy without strict mode seeing them.

### 109. Requirements for Oracle, SQL Server and MongoDB {#decision-109}

**Decided:** This is the last set of requirements per engine.

- **Oracle** reports each index type that its schemas' domain indexes use from outside those schemas (`CTXSYS.CONTEXT`, `MDSYS.SPATIAL_INDEX_V2`), as an `option`. It is checked against the image's `DBA_INDEXTYPES`. Omitting one leaves its indexes out of the copies and the image's schema.
- A domain index's own tables (Oracle Text's `DR$...$`) are left out of the catalog, since the index remakes them. Each copied schema's user gets `CREATE TABLE` so that it can.
- **SQL Server** reports none. It already refuses CLR-typed columns and full-text indexes.
- **MongoDB** reports none. It loads nothing into its server, and copies no Atlas Search indexes.

**Why:** Domain indexes are the only thing Oracle copies that an image may lack (the `-lite` image leaves components out). They need no privilege beyond `ALL_INDEXES`. Dropping an index never changes rows, so omitting one is safe. Reporting add-ons for SQL Server tables the adapter refuses anyway would only add a second thing to switch off.

### 110. Oracle keeps one container per run {#decision-110}

**Decided:** Working databases have a `close()`. A run calls it once its builds end, even after a failure, and after the requirements check.

The Oracle adapter keeps one container for its working databases' lifetime, mounting the run's directory. It plugs each stage's pluggable database in and out of that container in turn: for load, transform, logical packaging, restores and the image probe.

- A call whose paths lie outside the run's directory gets a container of its own.
- A failure stops the shared container, so the next call starts a fresh one.
- Starting the shared container first removes any that a killed run left behind.

Every other adapter's `close()` does nothing.

**Why:** An Oracle start takes about 35 seconds, before any work. A build started one container per stage: seven in one integration test, whose time was most of its limit. The stages run one after another on one image with the same settings, so they never need two servers at once. Plugging and unplugging is how the adapter already moved databases between containers.

### 111. The Salesforce adapter {#decision-111}

**Decided:**

- **Client.** The Salesforce adapter talks to the org only through a narrow client (`httpx`) of its REST, Tooling and Bulk API 2.0 interfaces.
- **Local copies.** It builds working copies, anonymization and images locally in SQLite. An object is a table, and `Id` is its key. The image is `logical`: JSON lines per object, with `objects.json`. There is no server, so images are not replicated as running databases.
- **The fake org.** The fake org ([decision 11](#decision-11)) is a SQLite file served over the same HTTP APIs, chosen by `connection: {fake: path}`. Tests, the conformance suite (including its CLI subprocess) and demonstrations run the whole adapter offline with it. A `fake` connection is accepted in any spec.
- **Cassettes.** Cassettes record that client's interactions. They are scrubbed of credentials, cookies, tokens, session IDs, instance URLs and other Salesforce hosts, and org IDs. They replay interactions in order, repeating the last response.
- **Recording.** `make record-salesforce ORG=<alias>` records a cassette from an org logged in with `sf org login web`. The committed cassette was first recorded from the fake.
- **Limits.** Polymorphic references are not foreign keys, so declare them as virtual ones. Compound and base64 fields are left out. Subsets use only the keys strategy, with long `IN` lists split across queries. Hooks and expression rules are not offered.
- **Packages.** Installed packages are `package` requirements, by namespace and version. `WorkingDatabases.available()` may answer None, meaning the working copies need no add-ons, so the build skips the check. Local copies hold a package's objects as ordinary tables.
- **Loading.** Loading records into an org (`bulk_load`) waits until seeding an org is designed.

**Why:** The run never holds org credentials ([decision 36](#decision-36)), so everything but re-recording must work without an org. Serving the fake over HTTP tests the real client too. Local copies avoid needing an org per build. A None answer keeps the requirement check meaningful for engines whose images run add-ons, instead of having Salesforce claim to provide every package.

### 112. Realistic data in the recording org {#decision-112}

**Decided:** The Salesforce org that cassettes are recorded from stands in for a source, so its data looks like production data.

- `make seed-salesforce` loads invented records whose emails, phones and websites look real, as Salesforce's own sample records do. The PII scan flags them.
- The recorder scrubs only credentials and the org's identity: tokens, session IDs, the instance URL and other Salesforce hosts, and org IDs. It keeps records as they are.
- The repository rule against real hostnames covers hosts that something connects to, not domains in invented sample records.

**Why:** Data in reserved example domains and fictional number ranges is exactly what the PII scan ignores, so a source holding it would test nothing. The records are invented. The org's own identity is what could reach a real system.

### 113. Salesforce fields hidden by field-level security {#decision-113}

**Decided:** The Salesforce catalog fails, before anything is extracted, on fields that field-level security hides from the connecting user. It names each one. The exception is a field listed in the connection's `hidden` setting (as `Object.Field`). Listed fields are left out of copies.

A field is hidden when all of these hold:

- the Tooling API's `FieldDefinition` lists it;
- some profile or permission set grants it (`FieldPermissions`);
- the object's description leaves it out.

Permissions are asked only about the fields the descriptions leave out, and the query is not grouped. Salesforce's `GROUP BY` on `FieldPermissions` left out fields that permission sets grant.

The org that cassettes are recorded from keeps a user who can read every field. A second cassette, recorded with that access removed, holds the failing catalog.

**Why:** A hidden field is missing from the description, so copies lacked it with no error, and strict mode never saw it. `FieldDefinition` also lists internal fields, and fields of features the org does not use, which no description includes. They are granted to no one, so `FieldPermissions` separates them from hidden fields. Both queries need the "View Setup and Configuration" permission, which the installed packages already need.

### 114. Codecov upload never blocks merges {#decision-114}

**Decided:** The coverage gate is CI's own `Integration coverage` job, which fails under 100% combined coverage. Uploading that report to Codecov is `continue-on-error`, with `fail_ci_if_error: false`.

**Why:** An outage or rate limit at Codecov, a third party, should not block merges. The gate it would report on has already passed or failed in our own job.

### 115. Search engines need the `monitor` privilege {#decision-115}

**Decided:** The Elasticsearch and OpenSearch adapters list the source's plugins to report them as requirements. That needs the `monitor` cluster privilege, on top of read access to the indices. The Elasticsearch README says so, and OpenSearch's points to it.

Without the privilege, the catalog fails with "listing the plugins failed", naming the cluster's error type and HTTP status.

**Why:** Plugins are what a working cluster must provide, and the nodes API is the only place they are listed. Asking for a broader privilege beats guessing plugins from mappings.

### 116. One token for the nightly runs {#decision-116}

**Decided:** `RUNNER_ADMIN_TOKEN` serves the nightly hosted runs twice:

- it registers the night's self-hosted runner;
- it starts the generated workflows.

The second use is needed because GitHub starts no `workflow_run` follow-on (promote after build, provision after fetch) for a run that the job's own `GITHUB_TOKEN` started.

**Why:** The night must test the follow-ons as a consumer's pipeline runs them. A second secret with the same reach would add a credential to rotate, without narrowing anything.

### 117. Salesforce's SQL suppression {#decision-117}

**Decided:** The Salesforce adapter's source has a per-file ignore of ruff's `S608` (SQL built from strings), with its reason, in the root `pyproject.toml`. The suppression budget ([decision 77](#decision-77)) counts only comments in Python files, so it does not count this ignore.

The adapter builds SOQL and SQLite statements whose object and field names come from the org's own descriptions, quoted. Every value is escaped (`soql.literal`) or bound as a parameter.

**Why:** Neither SOQL nor SQLite can bind identifiers, so statements must name objects and fields in text. A per-line `noqa` on every such statement would hide the reason in many places instead of stating it once.

### 118. Running the skill scenarios {#decision-118}

**Decided:**

- The skill scenarios (`make test-skills`) run on a copy of the plugin's files, with `datadouble` and its own Python installed inside that copy, under the copy's resolved path.
- They pin the judge model (`claude-haiku-4-5`).
- The shared skills rule forbids repeating a secret someone gives. The spec skill's description says so too, since a description is all an agent reads before calling a skill.

**Why:** Claude Code's evaluation sandbox reads only the plugin copy and the directories on `PATH`, and it matches paths literally (macOS's `/tmp` is a symlink). With the default model, its judge used an expired claude.ai login, although `ANTHROPIC_API_KEY` was set.

### 119. Tables that nothing selects {#decision-119}

**Decided:** A kept table that gets rows from no seed, `follow: all` or followed edge fails the plan by default (`subset.unselected: fail`). Two other values change that:

- `none`: the table is copied without rows, with a warning.
- `all`: every row is copied.

Seed keys take the same globs as excludes. A table's own name wins over a glob, and a table that two globs match fails the plan.

Jailer subsets behave as `none`. `import-jailer` writes that, and a spec cannot override it beside `subset.jailer`.

**Why:** A spec without seeds built empty images that passed verification. A table added to the lock later came out empty without anyone noticing. Failing loudly is [decision 10](#decision-10)'s rule.

Seeds are conditions in the source's own language, so `all` is the one way to copy whole tables in every engine. Jailer exports only what its subjects and associations reach, and [decision 81](#decision-81) runs a Jailer subset unchanged.

### 120. Engine compatibility lines {#decision-120}

**Decided:** An image's identity, its store path and its copies' `engine-version` label carry the engine's compatibility line. Each engine adapter declares the line's length (`Capabilities.version_line`):

| Line | Engines |
|---|---|
| Major version | PostgreSQL, SQL Server, Oracle |
| Major and minor version | MySQL, MongoDB, Redis, Elasticsearch, OpenSearch |

The image summary records the working server's exact version, and `k8s-pv` copies carry it as `server-version`.

**Why:** The first number of the version told MySQL 8.0 from 8.4 no better than nothing. Yet 8.0 cannot open a data directory that 8.4 wrote. So both ended up on one line, which `fetch`, `activate` and consumers treated as one series. Search snapshots likewise restore only on the same or a later minor version.

Within a line, the newest release opens what older ones wrote. Oracle is the exception: its pluggable databases plug cleanly only into the release update that wrote them.

A Helm chart renders before a claim binds, so the exact version is a label, for consumers that look it up first.

Amends [decision 22](#decision-22)'s "target engine version".

### 121. Accounts in physical images {#decision-121}

**Decided:** `package.accounts` lists the accounts that physical images, and so every copy of them, carry. Each account has a user, a password (plain text or a secret reference) and `privileges`.

**Privileges.**

- `all`, the default, is every privilege with the grant option (a PostgreSQL superuser).
- `read` is `SELECT` on the image's data:
    - MySQL: `SELECT, SHOW VIEW` on each of the image's databases.
    - PostgreSQL: `CONNECT` on each database, and `USAGE` plus `SELECT` on every table, view and sequence of each non-system schema. Never `pg_read_all_data` or `*.*`, which reach password hashes.
- `read` keeps what `PUBLIC` has. The image's `PUBLIC` privileges are left alone.

**Writing and checking accounts.** The engine adapter writes the accounts into the packaged data directory (`Capabilities.accounts`, the `Accounts` protocol). It resets an existing user's password and privileges. Before the image is stored, it logs in with each account over the network.

- MySQL also checks that a random wrong password is refused.
- PostgreSQL checks each role in `pg_authid`, and gets only a SCRAM verifier computed client-side.

**Refused names.** Adapters declare the names they can't use: length limits, reserved names and prefixes, Datadouble's own users, and names that must keep `all`, such as PostgreSQL's `postgres`. The planner refuses those names, duplicates, engines without accounts, and builds with no physical image.

**Passwords.**

- `run` resolves every password reference when it starts, before reading the source. `plan` alone resolves none.
- The package stage's inputs hold an HMAC of each account, under the run's anonymization key.
- `ci init` gives the build job the references. Plain-text passwords never become mounts.
- The image summary and build manifest name users and privileges, never passwords.

MySQL and PostgreSQL support accounts first. Amends [decisions 54](#decision-54), [62](#decision-62), [74](#decision-74) and [86](#decision-86).

**Why:** Consumers that serve a volume copy with the engine's official image can't set a password, since those images apply theirs only when they create a data directory. So nothing can connect over TCP. Instance copies carry the accounts too, but their start resets the engine's instance user: `datadouble` on MySQL, reserved for that reason, and `postgres` on PostgreSQL, documented.

Applications often also want a read-only account for reporting tools, and such an account shouldn't read other accounts' password hashes. A copy holds no personal data, so its passwords need not be secret. Teams that want a secret password keep it in a secret.

- Refusing names and unresolvable secrets before extract fails a build before hours of work, not at packaging.
- The keyed digest makes a changed password re-package, without the stage's inputs revealing it.
- A password in plain SQL could reach PostgreSQL's server log in a failing statement ([decision 54](#decision-54)).
- The build can't know an old password, so it checks that a wrong one is refused instead.
- PostgreSQL's working clusters trust the loopback address, where a port-forward arrives, so a login alone does not prove the password is checked there. The images keep `initdb`'s local and loopback `trust` lines, which are documented.

### 122. A read-only HTTP store {#decision-122}

**Decided:** A read-only `http` store (`datadouble-http`, its own package like `s3`) lets `fetch` read an export store that a web server serves.

**Reading.**

- Keys are URLs under `url`.
- A 404 means "not there". Any other error status fails, naming the URL path and status.
- Writes and deletes fail, naming the store.
- Archives are downloaded with `Accept-Encoding: identity` and written raw, so a server's compression cannot change what is hashed.

**Credentials and trust.**

- Credentials (`basic` or `bearer`) are secret references.
- `auth` requires an `https://` URL, so credentials never cross the network in clear text.
- `ca_bundle` replaces the default trust. The default is httpx's (certifi's bundle, or `SSL_CERT_FILE` and `SSL_CERT_DIR`), not the operating system's store.

**The index.** Stores that cannot list keys raise `ListingUnsupportedError` from `list`. `image_lines` and `generations` then read `<spec>/<group>/index.json`.

- `promote` rewrites each export store's index from a listing of that store, after each image's summary and channel.
- `prune --target export` rewrites it after pruning, and removes it when a group has nothing left.
- Because `promote` lists every export store, an S3 export store needs `s3:ListBucket` for `promote`'s credentials.
- The index is written to export stores only. Build stores have none.

**Why:** Web servers have no standard listing. Parsing Apache or nginx index pages depends on server configuration, and fetching without listing would lose newest-version selection.

Rebuilding the index from a listing, rather than editing it, means any later promote or prune repairs an index that is stale or missing. Concurrent promotes, or a promote that overlaps a prune, can each write a listing that misses the other's change, such as a new line. The next promote or prune corrects it.

### 123. A CI job to re-lock the source {#decision-123}

**Decided:** `ci init --job lock` writes a manual `lock` job on the production side. It uses the `build` runner, gets only the `sources` and `notify` sections' secrets, and has no schedule.

**What the job does.**

1. It runs `datadouble lock --output datadouble-lock`. That writes the new lock and `changes.txt` into that directory, and leaves the committed lock as it is.
2. It publishes the directory as the CI system's job artifact. Argo CD keeps no artifacts, so there the job prints both files to its log, and the termination message carries the outputs.
3. It never commits. A person checks the changes for personal data and commits the lock.

**The `lock` command.**

- `datadouble lock`'s response lists `changes` against the lock it replaces (`null` without a readable one), and the CLI prints them.
- `--output` naming the committed lock's own directory is refused, so the job can never overwrite it.
- An earlier lock that cannot be read is reported as such (`unreadable`), not as no lock.

**Why publishing a lock is safe.** A lock contains table, column and key names, engine type names and requirement versions, never data values. So publishing it as a CI artifact is consistent with [decision 18](#decision-18). Type names can carry schema literals, such as a MySQL `enum` or `set` column's labels. Those are part of the schema and already in the committed lock, so publishing adds no exposure.

The skills ([decision 105](#decision-105)) re-lock through this job where the person has no shell inside the boundary.

**Why:** Strict mode needs a lock, and locking reads the source. Where the only machine inside the boundary is a CI runner, re-locking needed a shell there, which pushed teams toward turning strict mode off. Reviewing the diff before committing is the check strict mode exists for, so the job publishes the diff rather than committing it.

Refines [decisions 10](#decision-10), [16](#decision-16), [18](#decision-18), [98](#decision-98) and [100](#decision-100).

### 124. Kubeconfig as a secret reference {#decision-124}

**Decided:** `k8s-pv`, `k8s-secret` and the `kubernetes` runtime take two optional settings:

- `kubeconfig`: a secret reference to a kubeconfig's content. It is `{env: NAME}`, or `{file: /path}` for a mounted secret file, which `ci init` mounts and the engine's user must be able to read.
- `context`.

**How Datadouble uses it.**

- Datadouble resolves the kubeconfig once.
- For each `kubectl` call, it writes the kubeconfig to a file (0600) in a new private directory (0700). The directory goes in `TMPDIR`, or in the home directory when `TMPDIR` is under the runtime's `mount`.
- It passes `--kubeconfig` and `--context`, and removes the file and directory when the call ends, even after a failure. A port-forward keeps its file while it runs.
- The CLI turns SIGTERM into `SystemExit`, so a cancelled job's cleanup runs too.

**Secret values.** The kubeconfig's string values of 16 characters or more are its secret values. `kubectl`'s output and errors have them replaced with `[redacted]`, and only they go to CI log masking.

**Failures and defaults.**

- An unresolvable kubeconfig fails with `unresolved_reference` before any work.
- `ci init` lists its variable like any secret reference, so only jobs whose spec sections name it receive it.
- Without it, `kubectl` uses the in-cluster service account or the ambient configuration.
- Exec credential plugins are not in the image.

**Why:** A CI job runs Datadouble in its image with only the paths in the spec, as the engine's user ([decision 86](#decision-86)). So a kubeconfig on the runner is neither visible nor readable. A secret reference travels like every other credential, from each system's own secret store.

A file per call, in its own directory, outside the paths pods mount, is never left behind by a failed or cancelled run. Masking a kubeconfig's structural lines would hide ordinary log text without protecting anything.

Amends [decisions 61](#decision-61) and [73](#decision-73).

### 125. Notifications in the spec {#decision-125}

**Decided:** Notifications are a spec section, `notify`, not a `ci init` option.

- `when` is `failure` or `warning`. The default is failures only.
- `to` lists `webhook` or `email` targets.

**Webhooks.** Datadouble posts webhooks itself, after the command of each generated job. The job names itself to the native step (`--ci-job`, or `ci.job` in JSON). Every job gets the webhook URL's secret.

The JSON body holds:

- the spec's name;
- the job;
- the outcome;
- the generation;
- the error code;
- the run's page (`run_url`, from each CI/CD adapter; null on Argo CD).

One webhook serves both sides of the boundary. A `{file:}` webhook becomes a mounted path in every job. Posts use the environment's proxy settings, verify TLS, follow no redirects, and send URL credentials only over https.

**Email.** Email is the CI system's own:

- Jenkins: `mail` in `post`, with `failure` and `aborted` for failures and `unstable` for warnings.
- GitLab: Pipeline status emails, listed in `instructions`.
- GitHub: its own run notifications, listed in `instructions`.
- Argo CD: refused by name.

**Failures.** A notification that fails to post is a warning line naming the target by position, never its URL, and it never changes the outcome. A missing webhook secret is a missing secret, like any other. A job that fails before Datadouble starts posts no webhook. The system's own failure notifications cover it, and on Argo CD `ci init` asks for an alert on failed Jobs.

**Why:** `ci init` is re-run whenever the spec, version or options change, and options live only in the generated files. So an option left off would drop notifications silently. The spec already describes an image's whole life ([decision 57](#decision-57)).

Posting from Datadouble works the same on every system, Kubernetes included. It knows the outcome, generation and error code, which a CI system's own step does not. Payloads follow [decisions 18](#decision-18) and [54](#decision-54).

### 126. Choosing the Docker command and socket {#decision-126}

**Decided:** The CI steps that run the Datadouble image (the GitHub action, the GitLab job template, the Jenkins step) find Docker like this:

- The Docker command comes from `DATADOUBLE_DOCKER`. The default is `docker`, and it must be one program without arguments.
- The daemon's socket comes from `DATADOUBLE_DOCKER_SOCKET`, else a `unix://` `DOCKER_HOST`, else `/var/run/docker.sock`.

An operator sets these in a runner's environment. `ci init --docker` and `--docker-socket` (an absolute path without `:`) write them into the generated configuration, which then takes precedence.

A step reads the socket's group from that path. When a socket is named, the step starts the image on that daemon (`DOCKER_HOST`, and podman's `CONTAINER_HOST`). It mounts the socket at `/var/run/docker.sock` inside the container, where `DOCKER_HOST` always points, so the `docker` runtime inside needs no setting of its own.

Rootless Docker is not supported. Argo CD's jobs are pods, and refuse both options.

**Why:** Runners with Docker installed outside `PATH`, or with another Docker-compatible daemon, could not run the jobs without editing generated files. Environment variables let an operator configure a runner once for every job on it. The options keep the configuration in `ci init`, as skills require.

Starting the image on the daemon it drives keeps one daemon, and one set of paths, for the image and the engines' containers. Rootless Docker's user namespace maps the engine's user to another host user. That breaks the socket's group, and the shared user that [decision 73](#decision-73) relies on.

Amends [decisions 73](#decision-73) and [100](#decision-100).

### 127. Jenkins jobs on Kubernetes agents {#decision-127}

*(Amended by [decision 133](#decision-133). Refined by [decision 140](#decision-140).)*

**Decided:** `ci init --agent kubernetes` runs a system's jobs as Kubernetes pods, instead of `docker run` on an agent with Docker. Docker stays the default.

**Which systems support it.**

- Each CI/CD adapter declares the agents it supports (`agents`, its default first). `ci init` refuses any other, and refuses `--cloud` for Docker agents.
- Jenkins supports it through the Kubernetes plugin.
- Argo CD's jobs are always pods.
- GitHub Actions and GitLab run only on Docker.

**Jenkins options.** They are named as for Argo CD ([decision 103](#decision-103)):

- `--runner` is the side's namespace.
- `--cloud` is the plugin's cloud. Without it, the plugin's default is used.

Each Jenkinsfile has an `agent { kubernetes { cloud; namespace; yaml } }` pod. The pod mounts that namespace's `datadouble` claim, and runs as the service account `datadouble-<spec>-<side>`. The two sides need separate namespaces, ideally separate clusters.

**Mounting the claim.**

- Jobs that start engine software need the spec's `kubernetes` runtime to name that namespace and claim. Every path in the spec must be under the runtime's `mount`, where the pod mounts the claim.
- Other jobs mount the claim at their paths' common directory.

**Users in the pod.**

- Only the pinned image's container runs as the engine's user.
- The plugin's agent container keeps its image's user, and starts with umask `0000`, so both can write the pod's workspace.
- The step runs `datadouble` in the Datadouble container.

**Boundary mark.** The mark is only the namespace's `datadouble-boundary` ConfigMap, mounted as an optional volume and read from the file.

**`instructions`** list:

- the namespaces;
- the plugin's cloud, and its credentials' rights;
- the claim (`ReadWriteMany`, or `ReadWriteOnce` with the runtime's `node`);
- the service account's RBAC.

**Helm chart.** The chart's `fsGroup` changes ownership only on a root mismatch.

**Why:** Installations whose Jenkins agents are pods often have no Docker. The `kubernetes` runtime ([decision 73](#decision-73)) needs Datadouble in a pod that shares the engine pods' claim at the same path, which `docker run` cannot give. Taking the namespace and claim from the side's own `--runner`, as Argo CD does, keeps a non-production pod off production's claim and boundary mark.

- The agent container's SCM checkout over SSH needs its own account and home. So only Datadouble's container takes the engine's user ([decision 86](#decision-86)).
- The umask is what lets two users share the workspace, whose `sh` control directories the agent creates.
- Jenkins lays its own environment over a container's, so an environment variable could mark a pod. A mounted ConfigMap keeps the mark the namespace operator's.
- A recursive `fsGroup` change would walk a terabyte-scale claim on every start.

Amends [decisions 100](#decision-100) and [102](#decision-102).

### 128. Several databases from one server {#decision-128}

**Decided:** One source may read several databases from one server.

**Choosing databases.** MySQL's and PostgreSQL's `connection.databases` lists names or globs. `["*"]` means all but the server's own databases and templates. Each entry must match. The default is the URL's or DSN's database.

**Table names.** A table name is its namespace path, outermost first, plus its name. `TableName.namespace` is a tuple, fixed in length per engine:

- MySQL: `database.table`.
- PostgreSQL: `database.schema.table`, even with one database.
- Other engines keep their own table names.

**Matching names.** Spec names and globs match from the right, part by part. `*` stays within a part, and a table's own dotted name matches whole too. A name that fits several tables fails, naming them.

**Snapshots.**

- MySQL reads every database under its one consistent snapshot.
- PostgreSQL reads each database under its own exported snapshot, opened together, and records them in the manifest's `snapshots`. It refuses virtual references between databases (the planner's `joins_outermost` capability).

**Copies.** Working databases, images and copies hold every database under its own name. That includes a database whose every table is excluded, which is empty.

- MySQL views over a database that was not read are left out of the copies, as are views over excluded tables and foreign keys into such a database.
- Hooks and assertions run in a default database: the connection's own when it was read, else the first one read. Copies announce it. MySQL's `before_load` hooks run in the default database, which is created first.

**Why:** Applications spread over many databases on one server need one snapshot, one image and checked references between them, which a spec per database cannot give.

A typed path keeps the core free of what each level means, and fits SQL Server's and Oracle's databases later. Matching from the right keeps short names working. Failing on ambiguity follows the fail-loudly principle, rather than guessing which table was meant.

PostgreSQL cannot share snapshots or join across databases, so each database is consistent on its own and no reference may cross. MySQL's snapshot spans the server, so it keeps [decision 5](#decision-5) whole.

Amends [decisions 9](#decision-9) and [79](#decision-79).

### 129. Several sources in one spec {#decision-129}

**Decided:** A spec lists its sources under `sources`, a map from a name to a source, even with one server.

**What the sources share.**

- Every source has one engine, and all give the same `image` and `server`, since one working server holds them all.
- The working server runs the newest of the sources' versions.

**Snapshots.** Each source is read under its own snapshot. The snapshots are opened one after another, before anything is read. The manifest records each source's snapshot and when it was opened.

**One merged catalog.** The sources' catalogs merge into one, for planning and the lock. The plan fails when:

- a database (outermost namespace) is in two sources. Each session reports the databases it reads (`SourceSession.namespaces`), so empty ones collide too;
- sources are on different compatibility lines (`Capabilities.version_line`, [decision 120](#decision-120)). For example, 8.0 and 8.4 are separate MySQL lines;
- a second source is given for an engine without the planner's `several_sources` capability. PostgreSQL and MySQL have it;
- two sources use a requirement at different versions;
- some sources omit a requirement that others use.

Messages about requirements name the source that uses them.

**References between sources.** A virtual reference between sources is followed with the keys strategy. Its keys are read through each side's snapshot. It is listed in the plan's and manifest's `between_sources`.

**Extract and load.** Each source's snapshot gets the plan restricted to its tables (`Plan.of_source`), and writes its own extract directory. `WorkingDatabases.load` takes every source's extract, in the spec's order, and runs hooks in the first source's default database.

**Why:** Applications keep related databases on several servers of one engine, and need them in one image and one development server. No snapshot spans servers, so recording each one keeps anybody from taking the image for one point in time.

Merging catalogs keeps names, groups, strict mode and anonymization exactly as for one source, and one key per run keeps equal values equal across sources. A per-source view of the plan lets adapters extract unchanged. Only loading had to learn several extracts, so engines can add the capability one at a time.

Amends [decisions 5](#decision-5), [41](#decision-41) and [72](#decision-72).

### 130. Strict mode levels per kind {#decision-130}

**Decided:** `strict` is a mapping with a level per kind:

| Kind | Covers | Levels | Default |
|---|---|---|---|
| `data` | tables, columns and requirements | `off`, `tables`, `columns` | `columns` |
| `functions`, `views`, `triggers`, `types`, `sequences`, `events` | that kind of object | `off`, `warn`, `fail` | `warn` |

**Setting it.**

- Kinds left out take their defaults. A bare level is refused.
- On the command line, `--strict KIND=LEVEL` sets one kind, and can be repeated.
- `promote.strict` takes the same mapping, compared kind by kind.

**What breaks fails.** A difference that breaks something fails at every level, whichever kind it is. Strict mode takes a hook from planning that says what a difference breaks.

**Tables and columns.**

- Changed known columns ([decision 10](#decision-10)) fail at `data: columns`, and warn at `tables` and `off`, unless they break their anonymization rule.
- Removed tables and columns, and changed keys, always fail.

**Objects.** Engine adapters report a source's objects (functions, procedures, views, triggers, types, sequences, events). Each comes with a fingerprint of its definition, and with dependency edges. Edges that come from parsing bodies are marked `inferred`.

- The lock records objects. Objects are part of a catalog's hash only when the catalog has some.
- Definitions are fingerprinted as written, apart from line endings and trailing spaces.
- A removed object breaks a remaining object that depends on it by an exact edge (one that does not need an excluded table).
- A removal that only inferred edges point at warns at every level. Inferred edges match by kind, namespace and name, not signature.
- Objects whose exact edges name only excluded tables are ignored, as those tables are.

The MySQL adapter needs MySQL 8.0.13 or later.

**Why:** Planning reads the live catalog, so a widened column breaks nothing. Failing on it everywhere taught people to turn strict mode off. What matters is whether a change breaks something.

A changed trigger or function can change what a copy holds. But most object changes are routine, so they warn by default, and each kind can be made to fail. Only package planning ([decision 131](#decision-131)) knows everything an image needs, so the check is a hook it fills in.

Amends [decision 10](#decision-10).

### 131. Package planning and standalone groups {#decision-131}

**Decided:** Package planning runs after the subset plan, once per image group. It works out each group's **closure**: everything the group's image needs.

**Where the closure starts.**

- The group's tables.
- The objects that come with them: triggers on them, views and other objects whose exact table edges are all in the image, and triggers on such views.
- The objects `objects:` declares at the spec and group level. They are written `{kind: name}`, matched from the right like tables' names, with optional argument types.
- For a standalone group, the objects its `include` patterns match.

**What the closure follows.**

- References whose parents are followed. Fallback edges are not followed.
- `follow.down` edges, from the group's own tables too.
- Tables' edges to objects.
- Objects' exact and inferred edges.

It follows all of these across namespaces.

**Table dependencies.** Catalog tables list what they need besides foreign keys (`Table.depends_on`). That is objects, and tables such as those a row-level security policy reads, a typed table's type, or an inheriting table's parent. It is hashed only when present.

**Placing objects in groups.**

- Spec-level `objects` are roots of every group. So in a group that is not standalone, they need their tables there too.
- `include` patterns place objects as they place tables.
- A non-standalone group that needs an object only groups it does not require include fails the plan. The exceptions are the objects that come with its own tables, and its declared objects.
- A declared object of a non-standalone group, or one it needs, that needs a table neither the group nor a group it requires holds also fails the plan.

**Standalone groups.** A `standalone: true` group cannot have `requires:`. It pulls what its closure reaches into its image, and holds exactly that closure. A group that is not standalone pulls nothing in.

**Rows of pulled-in tables.** A standalone group's `rows:` mapping sets a pulled-in table's rows. It is keyed by a table name or glob, or by a reference `child.column -> parent`, which wins. The values are:

- `referenced`: the default for tables a reference reaches. These are the rows the image's rows reference, and those referencing them along followed down edges, transitively.
- `all`: every source row, and what those rows reference.
- `none`: structure only. References into the table are left out.

A table reached only through objects defaults to `none`, with a warning. Structure-only tables take no rows.

**Reading pulled-in rows.**

- Pulled-in rows are read by keys in the same snapshot, from the extract's conditions taken stand-alone.
- They go into an extract per group (`groups/<group>`). That extract also holds the group's own tables that gained rows.
- Their own references (followed, `drop` and followed-down ones, among own tables too) are followed until nothing changes.
- Tables the subset keeps without rows never gain any.
- A gained row whose `drop` reference points at a parent without rows is dropped. So are rows whose parents were dropped that way, until nothing changes. Under the nested strategy this costs one keys pass over the whole subset.
- `load` takes those tables from the group's extract, and others from the shared extract, so other groups are unaffected.

**Failures.** An object that needs an excluded table, or one the catalog lacks, fails the plan, naming the path. It warns instead when an inferred edge is on the path.

**Strict mode.** Strict mode's `Breaks` hook fails:

- a removed object that a packaged table (by its locked exact edges) or a declared root needs;
- a new or changed object on a failing closure path.

**Engine support.** The capability `standalone` marks adapters that build standalone images: PostgreSQL, MySQL and SQL Server. Other adapters refuse a standalone group that pulls anything in, or whose source has objects.

**Why:** Each image should work when served alone, and only per-group dependency resolution can tell what that needs.

- Reading pulled-in rows into a per-group extract keeps the shared extract and the other groups exactly as they were. It works under either strategy, and needs no engine to filter rows in place. Reading by keys is what strategy B already does.
- Not following fallback edges keeps the subset model's meaning: the parent may be missing.
- Placing objects with the same patterns as tables keeps one model of what a group holds.
- Dropping objects without `CASCADE` makes a dependency the catalog missed fail loudly, naming what blocked it. PostgreSQL's aggregates, operators and casts are not in the catalog. Tables' index expressions and row-level security policies are.
- Inferred edges stay warnings ([decision 130](#decision-130)).

Refines [decision 41](#decision-41).

### 133. Default namespace for the Kubernetes runtime {#decision-133}

**Decided:** The `kubernetes` runtime's `namespace` is optional. Left out, pods start where `kubectl` would put them unasked:

- **Inside a cluster** (no kubeconfig in the runtime's settings, `KUBECONFIG` unset or empty, and a service account namespace file in Datadouble's pod): the namespace of Datadouble's own pod, read from `/var/run/secrets/kubernetes.io/serviceaccount/namespace`.
- **Outside a cluster:** the kubeconfig context's namespace, or `default`.

When a spec sets a namespace, `ci init` for Jenkins Kubernetes agents and Argo CD refuses a side whose `--runner` differs, for jobs that start engines (`runtime_namespace` in the core).

`instructions` give a Jenkins Kubernetes cloud's credentials the Role that the Kubernetes plugin documents: pods with `patch` and `update`, `pods/exec`, `pods/log`, and watching events. These rules are held as data, with the service account's rules (`CLOUD_RULES`, `ACCOUNT_RULES`, `VOLUME_RULES`).

**Why:** Each side runs in its own namespace, named by `--runner`, and one spec serves both sides. So the spec cannot name a namespace that is right for both. The pod running Datadouble already knows its own namespace, as `kubectl` does. Following `kubectl` outside a cluster is the least surprising fallback. The CI/CD conformance suite grants exactly the Roles that `instructions` list, so they must be what the plugin needs.

Amends [decisions 73](#decision-73), [103](#decision-103) and [127](#decision-127).

### 134. File mode for the filesystem store {#decision-134}

**Decided:** The filesystem store takes `mode`, an octal string of three digits with an optional leading `0`. The default is `"0640"`.

- **Files.** Every file the store writes gets that mode with `chmod`, after it is written and before it is renamed into place. So the umask does not apply.
- **Directories.** Every directory the store creates, including its root, gets the mode plus the search bit wherever the mode has the read bit. For example, `0640` gives `0750`. Directories that already exist are left alone.
- **Refused modes.** Modes that give the group or others write access, or leave the owner without read and write, are refused when the store's settings are parsed, naming `mode`.
- Only the filesystem store has `mode`.
- `fetch`'s staging store is fixed at `0600`, so the channel files `fetch` writes through it stay `0600`. The rest of staging follows the umask.

**Why:** Files written through `mkstemp` were `0600`, so a web server serving an export store as its own user could read nothing `promote` wrote. Group read by default covers the usual setup (the server in `promote`'s user's group) without making images world-readable, and `"0644"` is one setting away.

Search follows read on directories, so whoever can read a file can reach it. Changing directories the store did not create would override an operator's choice. A mode that others can write would let another user change or remove what the store holds.

### 135. Where `k8s-secret` announces and withdraws {#decision-135}

**Decided:**

**Default namespace.** The `k8s-secret` announce adapter's `namespace` is optional. Left out, it resolves as the `kubernetes` runtime's does ([decision 133](#decision-133)), with the same helper and the adapter's own `kubeconfig` and `context`:

- **Inside a cluster** (no kubeconfig in the settings, `KUBECONFIG` unset or empty, and a service account namespace file): Datadouble's own pod's namespace.
- **Outside a cluster:** the kubeconfig context's namespace, or `default` when the context names none.

A context that `kubectl` cannot read (a bad kubeconfig, an unknown context) fails, naming the kubeconfig and context. This applies to the runtime too.

**Recording what was announced.** The copy registry records, for each `announce` entry, the targets the entry reported for a copy (`Copy.announced`).

- Entries are keyed by the adapter's name, with `name#2` for its second entry, and so on.
- One announcement may cover several copies, so each copy records all of the entry's targets.
- An adapter that fails part way reports what it wrote before failing (`AnnounceError.written`). That is recorded too, as is what earlier entries wrote.
- A failure to record is added to the adapter's error, rather than replacing it. After a successful announcement, it fails the command, naming every target announced but not recorded.

**Withdrawing.** `withdraw` gives each entry only its own recorded targets. `k8s-secret` deletes each Secret from the namespace recorded for it. It resolves its own namespace only for copies with nothing recorded.

Withdrawing is best effort and complete:

- Every adapter tries every target.
- The core makes and withdraws each entry on its own. So an entry that cannot be made (a missing token, an adapter no longer installed), or that fails with an `OSError` or `ValueError`, does not stop the others.
- Then it raises one error naming each announcement left behind. The error gives an entry's recorded targets when the adapter could not name them, and every target recorded under a key that no current entry has.
- The copies are already gone from the registry, so a person removes those leftovers by hand.

**Limits.**

- Entry keys are positional per adapter. Reordering or removing entries of the same adapter while copies are live leaves their announcements to be removed by hand.
- Every process that announces or releases with one spec must reach the same cluster through that entry.
- The releasing identity needs `delete` on Secrets in every namespace a copy was announced into.
- Known gap: a copy released between its announcement and the recording is withdrawn without the record, so in the resolved namespace.

**Why:** One spec serves both sides of the boundary, each in its own namespace. So an announce adapter that defaults to a fixed namespace forces every spec to name one. And `default` is a namespace that neither the Helm chart's Role nor the Jenkins `instructions` grant. Following the runtime keeps one rule for where Datadouble's Kubernetes objects go.

- A resolved default differs between processes (a laptop's context, a pod's own namespace). So withdrawing must use where the Secret went, not where the sweep would put it, or a copy's credentials would outlive the copy.
- Recording per entry keeps one entry from deleting by another's names. Targets do not name clusters, so the same-cluster rule is documented rather than checked.
- One unreachable namespace must not strand every other copy's credentials. Since released copies leave the registry, the error is the only place left to say what remains.
- Quietly falling back to `default` when a context cannot be read would write or look for Secrets in the wrong place.
- The release race is narrow, and left documented.

### 136. Dependency ranges, exact versions in the lock {#decision-136}

**Decided:** Requirements in `pyproject.toml` are ranges, normally a lower bound only. Exact versions live in `uv.lock`.

- Zensical's requirement is `>=0.1.1`.
- Dependabot's uv updates raise lower bounds, whatever `versioning-strategy` says. So when their changes are applied by hand, only `uv.lock` moves.

**Why:** Exact pins in `pyproject.toml` duplicate the lock and turn every release into a pull request. The lock already makes builds reproducible. A release that breaks the site fails `make docs` in CI before it can merge.

Amends [decision 46](#decision-46).

### 137. YAML is always block style {#decision-137}

**Decided:** YAML mappings are always written in block style, one key per line, never as flow mappings (`{path: /srv, local: {node: a}}`). This applies everywhere:

- specs;
- docs and skill examples;
- test fixtures;
- Helm values and templates;
- everything `ci init` generates.

A sequence item that is a mapping is written `- key: value`. Empty `{}` and `[]` stay. So does a short flow sequence of plain scalars (`include: [billing.*]`). A flow sequence holding a mapping does not.

`scripts/format_yaml.py` rewrites flow mappings in YAML files, Markdown `yaml` fences and Python triple-quoted strings. `make lint` runs it with `--check`.

**Why:** Block style reads the same in a diff, a review and a terminal. It takes a comment on any key, and never needs its braces counted.

### 138. CI pulls Docker Hub images through a mirror {#decision-138}

**Decided:** CI pulls Docker Hub images through Google's mirror, `mirror.gcr.io`, in every job that pulls images. These are the integration groups, the CI/CD conformance suites, the privileged k3s and btrfs jobs, the nightly hosted run, and the PR and release image builds.

**How each piece gets the mirror.**

- **The runner's Docker daemon.** A composite action (`.github/actions/docker-mirror`) adds the mirror (`registry-mirrors`, merged into `/etc/docker/daemon.json`) and restarts the daemon right after the checkout.
- **Buildx builders** get it from `.github/buildkitd.toml`.
- **k3s**, whose containerd pulls its own images, gets it from `compose.mirror.yaml`.
- **Kind's node** needs no mirror. The suites load every Docker Hub image its pods run from the runner's daemon, and Argo CD's images come from other registries.
- **Docker-in-Docker** gets none, because CI never starts it (`DOCKER=host`).

**Egress.** Each job's egress list adds `mirror.gcr.io:443`. That host serves its tokens, manifests and blobs itself; a blob's redirect stays on that host. The list keeps Docker Hub's endpoints, since Docker, BuildKit and k3s fall back to Docker Hub when the mirror fails or lacks an image.

Images are pinned by digest, so the mirror cannot change what a job runs.

**Why:** Anonymous Docker Hub pulls share a rate limit per IP address across hosted runners. A run that hits it (`429 Too Many Requests`) fails every test that needed an image it hadn't pulled yet. Google's mirror needs no account or secret, and is a public pull-through cache of Docker Hub. Logging in to Docker Hub would need a secret that fork pull requests don't get.

### 139. Engine messages hide only data values {#decision-139}

**Decided:** Engine messages hide only data values, not every quoted string.

**Data-bearing message shapes.** Each SQL engine adapter lists, in one place, the message shapes its engine uses to report data values:

- PostgreSQL: `invalid input syntax for type integer: "…"`, `Key (email)=(…) already exists`, `Failing row contains (…)`.
- MySQL: `Duplicate entry '…' for key`, `Incorrect integer value: '…' for column`, `near '…' at line 1`.
- SQL Server: `The duplicate key value is (…)`, `converting the varchar value '…'`, `Truncated value: '…'`, `character string '…'`.
- Oracle: ORA-01722, ORA-01438, and the ORA-03301/03302 details.

**Object names.** Each adapter also lists the words after which a quoted string names an object: `table`, `column`, `constraint`, `role`, `file` and the like, plus MySQL's `'user'@'host'` and `in 'field list'`.

**What the `Redactor` does.** The core's `Redactor`:

- hides those data values, each within its line. A value running over a line break is hidden too, up to its last closing text;
- keeps those object names;
- hides every other quoted string, including introducers such as `N'`, `E'` and `_utf8mb4'`.

**Messages a user raised.** A message a user raised hides every quoted string, names included. Datadouble recognizes one from the driver's code or, in a tool's output, from the code in its text:

- PostgreSQL: SQLSTATE P0001, or a context naming a procedural language (`PL/pgSQL`, `PL/Python`, `PL/Perl`, `PL/Tcl`).
- SQL Server: error numbers from 50000.
- Oracle: ORA-20000 to ORA-20999.

**MySQL.** MySQL keeps names only in a message that matches, whole, the server's own text for its error number. The adapter holds a table of those texts. Every quoted string is hidden in:

- a message with another number, or other text;
- in the client's output, error 1644 or a SQLSTATE of class 45.

A trigger can SIGNAL with any number, a real one included, and pymysql drops the SQLSTATE. In the client's output, an `ERROR n (state)` line followed by another line before the next error also counts as raised.

A trigger that reproduces a server message word for word keeps its names. That residual is accepted, since it can only put a value where that message puts a name.

**Container runtime failures.** When a failed tool's exit status is 125, 126 or 127 (`runtime_failure` in the core), its output is not redacted. It is trimmed and summarized like any other tool output.

**MySQL passwords with line breaks.** A MySQL connection URL whose password holds a line break is refused, naming the URL and not the value. `mysqldump` reads the password from an option file that holds one option per line.

**Why:**

- Database messages quote object names far more often than data values. An error is only useful when it says which table or constraint failed, which [decision 54](#decision-54) wants.
- Listing the data-bearing shapes per engine, from each engine's own message texts, makes a newly found shape a one-line addition. Hiding anything unclassified keeps a message nobody has looked at safe.
- A trigger or procedure can put any value in its own message, quoted after any word, so only hiding everything there is safe.
- A line break in a MySQL password would split it into a second option line, which `mysqldump` then reports, value and all, as an unknown option.
- `docker run` and `podman run` exit with 125 when they fail before the program starts (an image that cannot be pulled, an option the daemon refuses). They exit with 126 or 127 when the command cannot be run or is not found, as `docker exec` and a shell do. That covers the `kubernetes` runtime's `sh` wrapper too. That output is the container runtime's and holds no data. Hiding it hid the image's name in `Unable to find image "postgres:18.7-trixie" locally`.
- The engines' own tools exit with small statuses, so a program's own failure is not mistaken for the runtime's.

Amends [decision 54](#decision-54).

### 140. Adapters declare their remote settings {#decision-140}

**Decided:** An adapter declares the settings that name paths not on the machine running the job. `ci init` never mounts them.

**Declaring.** The adapter wraps its entry point in `datadouble.settings.remote`, giving each setting as a dotted key inside its settings, such as `@remote("nfs.path")`. An engine's keys are inside the source, such as `connection.patterns`. A key covers its whole value, list or mapping included. The wrapped entry point still builds the adapter as before.

**Using it.** `ci init` asks every installed adapter for its declared settings. Where an adapter is named in the spec (a source's engine, `files`, each store, `replicate.using`, each announcer, `runtime`), its declared settings are left out before the job's sections are searched for absolute paths. Secrets are still found in them.

**The declared settings:**

- `k8s-pv`: `nfs.path`, the export's path on the NFS server.
- `s3`: `prefix`, a key prefix in the bucket.
- `vault`: `mount` and `path`, inside Vault.
- `redis`: `connection.patterns`, key patterns on the source.

**The `files` section.** The core searches only its `connection`. The `select` tests, `exclude` names and `paths_from` paths are relative to the tree, and the `cleanup` commands run in the cleanup image.

**Why:** `ci init` mounts every absolute path a job's sections name, since only the adapter knows what its settings mean. A path on another machine then became a mount. On a Docker agent the job refused to start, or mounted an unrelated directory. On a Kubernetes agent `ci init` refused the spec, because the path was not on the work claim.

- Only the adapter knows which of its settings are elsewhere. A declaration on its entry point lets an adapter from outside this repository say so too, without the core knowing its name.
- Declaring the few remote settings keeps every existing local path mounted. Declaring the local ones instead would have every adapter list them, and a forgotten one would break a job at run time.
- Settings that are URLs (`s3://`, `https://`) never looked like paths, so they need no declaration.

Refines [decisions 100](#decision-100) and [127](#decision-127).

## Open

No questions are open at present.
