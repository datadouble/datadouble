# Contributing to Datadouble

Thanks for your interest. Datadouble is MIT licensed; by contributing you agree
that your contributions are licensed the same way.

## Before you start

Read [AGENTS.md](AGENTS.md) and [docs/decisions.md](docs/decisions.md). Decided
items are settled unless a pull request makes the case for reopening one.

## Writing documentation

Documentation follows [WRITING.md](WRITING.md): explain the problem before the
mechanism, set up every idea before using it, and keep sentences and
paragraphs short. Readability never costs accuracy.

## Before opening a pull request

1. Run the full check suite (typing, lint and tests, as described in
   `TESTING.md`) and confirm it passes.
2. Update `CHANGELOG.md` under `[Unreleased]` for any user-visible change, and
   for any security fix even when nothing visible changes. See
   [Changelog](#changelog).
3. Update the design documents and `docs/decisions.md` if your change alters
   behavior they describe or makes a new decision.
4. Start every new source file with the copyright and SPDX lines described in
   [AGENTS.md](AGENTS.md).
5. In Markdown, indent nested list items four spaces past their parent's
   marker. GitHub accepts two, but the documentation site's Markdown parser
   flattens them into the parent list. Run `make docs` to check.
6. Write YAML mappings in block style, one key per line, never as flow
   mappings like `{path: /srv, local: {node: a}}` (decision 137). This holds
   for YAML files, `yaml` fences in Markdown, YAML in Python strings and any
   YAML code generates. Empty `{}` and `[]`, and short flow sequences of
   plain scalars such as `include: [billing.*]`, are fine. Run
   `uv run python scripts/format_yaml.py` to rewrite flow mappings in place;
   `make lint` runs it with `--check`.

## How pull requests merge

Most pull requests fix one issue. They are squash-merged, so each becomes one
commit on `main` whose message ends with `Fixes #N`.

Sometimes several issues touch the same code, or would conflict in
`CHANGELOG.md` and `docs/decisions.md` if they merged one at a time. Then one
pull request can fix them all, with one commit per issue. Each commit's
message ends with `Fixes #N` for its own issue. These pull requests are
merged with **Rebase and merge**, never squashed, so each issue keeps its own
commit on `main`.

Before such a pull request merges:

- Fold review fixes into the commit they belong to (`git commit --fixup`,
  then `git rebase --autosquash`), so that no commit only repairs another.
- Make each commit pass the check suite on its own, since `git bisect` can
  land on any of them. `git rebase -x 'make check' origin/main` runs it on
  every commit.

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries
accumulate under `[Unreleased]` until a release; the release script moves them
into a dated version section and refuses to cut a release when `[Unreleased]`
is empty.

**What gets an entry:** anything a user would want to know before upgrading.
That is usually visible behavior, but it also covers security work nobody can
see: a dependency bump that carries a CVE fix, a toolchain bump that clears a
standard library advisory, or a fix to the build and release pipeline. Those go
under `### Security`. Without the entry a security-only release cannot be cut,
and it is the only place users learn why a patch release is worth taking.

Routine dependency bumps with no security content don't each need an entry.
Summarize them in one line under `### Changed` when a release is cut anyway, or
leave them out.

**Sections,** in this order, each only when it has entries: `### Security`,
`### Added`, `### Fixed`, `### Changed`, `### Deprecated`, `### Removed`.

**Each entry** opens with a bold sentence saying what changed for the user,
then explains why and what to do about it, and links the issue or pull request.
A change that can break existing users starts with **BREAKING** and says who is
affected and how to adapt:

```markdown
### Fixed

- **`promote` no longer accepts an image whose manifest lacks a strict-mode
  level** ([#42](https://github.com/datadouble/datadouble/issues/42)). ...
```

## A note on AI-assisted contributions

We use AI tools in our own development and welcome others who do the same. Pull
requests must still show human understanding of the change: explain why it is
needed, not only what it does, and how you tested it. Low-effort submissions
that appear to be unreviewed AI output will be declined.
