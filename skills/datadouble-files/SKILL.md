---
name: datadouble-files
description: Set up a Datadouble spec's files section, which copies file trees (uploads, attachments) alongside a database - choosing files with find tests, following the paths the copied rows name, and cleanup commands. Use when someone needs files copied with their data, or asks what a find expression selects.
---

# File trees

Follow `../RULES.md` throughout.

A spec's `files` section treats each top-level directory under
`connection.root` as a table of files keyed by path (decision 97):

- `select` gives a directory GNU `find` tests (`-mtime -30`, `-name '*.pdf'`),
  or `-false` to take only the files rows name.
- `paths_from` follows the database subset: a template over a table's columns
  (`documents/{customer_id}/{file_name}`), with an optional `match` pattern
  whose first group is the path, for values such as URLs.
- `exclude` names directories to leave out; every other directory must be
  selected, or the plan fails.
- `cleanup` commands run on the copy only, in `image` (for example exiftool
  to strip photo metadata). The PII scan still reads every file afterwards.
- A copy of a live tree is inconsistent unless it is snapshotted; the spec
  must say `accept_inconsistent: true`.

## Explain, then try

For each `select` expression, say in plain words what it chooses before
writing it ("files changed in the last 30 days, except logs"). Then try it
against a sample directory the person names, with the same GNU find the
image uses: `find <sample>/<directory> -type f <tests> -print`. Show what it
picked, and only then write it into the spec. Expressions that act on files
(`-delete`, `-exec`) are refused; they belong in `cleanup`.
