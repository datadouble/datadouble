# Contributing to Datadouble

Thanks for your interest. Datadouble is MIT licensed; by contributing you agree
that your contributions are licensed the same way.

## Before you start

Read [AGENTS.md](AGENTS.md) and [docs/decisions.md](docs/decisions.md). Decided
items are settled unless a pull request makes the case for reopening one.

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
