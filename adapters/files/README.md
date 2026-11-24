# datadouble-files

The file tree engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It copies a spec's `files`: the uploads, documents and other files a
database's rows refer to, as a second image beside the database's, in the
same generation and image groups.

```yaml
files:
  connection:
    root: /srv/uploads             # the tree, as Datadouble sees it
    accept_inconsistent: true      # copy each file as it is when read
  select:                          # find tests for each top-level directory
    avatars: -mtime -30
    documents: -false              # none of its own: only what paths_from names
  exclude: [tmp, cache]
  paths_from:                      # the files the chosen rows name
    table: customer_documents
    path: '{url}'                  # a template over the row's columns
    match: '^https://cdn[.]example[.]com/uploads/([^?]+)'   # group 1 is the path
  cleanup:                         # shell, run in the copy's root
    - find . -name '*.log' -delete
    - exiftool -all= -overwrite_original -r ./avatars
  image: vendor/exiftool:13        # the image cleanup runs in; Debian by default
```

Each top-level directory is a table whose rows are its regular files, keyed
by their path from the root. Every top-level directory must be selected or
excluded, so a new directory fails the plan rather than being copied unseen
or left out unnoticed. Selection tests are GNU `find` tests; tests that act
(`-exec`, `-delete`, printing) or follow symbolic links are refused, and only
regular files are copied. `:as_of` in a test stands for the plan's anchor
time, as in `-newermt :as_of`.

`paths_from` specifies how file paths come from the source's selected rows, by a path
relative to the root. `path` is a template over a table's columns, such as
`documents/{customer_id}/{file_name}`, and `match`, if given, a regular
expression whose first group is the path, for values such as URLs or keys
with a prefix. `paths_from: customer_documents.path` is short for a column
that holds the path itself. The values are read inside the source's
snapshot, and those files are copied too. A path that leaves the tree, or
names no file, is skipped; values the pattern does not match are counted in
a warning, since they usually mean the pattern is wrong.

Cleanup is not proof: the PII scan reads every copied file's path and the
runs of printable text in its bytes, which includes a photo's EXIF fields
and similar metadata, and fails the image if it finds personal data.

A consistent copy needs a filesystem snapshot (btrfs, ZFS or LVM), which this
version does not take, so the extract runs only with `accept_inconsistent:
true`, and images record that their files were copied as they were when read.
