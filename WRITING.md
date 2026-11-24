<!-- Copyright (c) 2026- Tight Line LLC -->
<!-- SPDX-License-Identifier: MIT -->

# Writing Datadouble's documentation

Datadouble's documentation is read by people deciding whether to use it, people
setting it up, and people working out why a run did what it did. All of them
are busy, and most of them are new to at least some of the ideas. Write so
that a capable engineer who has never seen Datadouble can follow every page
from top to bottom without rereading a sentence.

This guide applies to everything under `docs/`, the READMEs, and the text that
generates the reference pages (the spec's field descriptions and the CLI's
help).

## 1. Tell a story the reader can follow

Flow matters more than anything else in this guide. A page is not a pile of
correct facts; it is an explanation, and the order is part of the explanation.

- **Start with the problem.** Before describing how something works, say what
  problem it solves and who has that problem. A reader who knows why a thing
  exists can hold its details in their head; a reader who doesn't, can't.
- **Set up before you present.** If a section depends on an idea, introduce
  the idea first. Never use a term and define it later, and never describe a
  mechanism before the reader knows what it is for.
- **Make the outline visible.** Open a long page with a short paragraph that
  says what the page covers and in what order, and why that order. Headings
  should read as a sensible sequence on their own.
- **Move from simple to detailed.** Give the common case first, completely.
  Then the variations. Then the edge cases and exceptions. Rarely needed
  detail goes last, or in an admonition, or on another page.
- **Connect the sections.** The first sentence of a section should say how it
  follows from the one before: "That covers which rows are copied. The next
  question is which other objects an image needs."
- **One page, one job.** If a page explains two unrelated things, split it or
  link out. Don't repeat a long explanation on several pages; explain it once
  and link to it.

Ask of every section: does the reader know why they are reading this, right
now, here? If not, move it or set it up.

## 2. Sentences

- **One idea per sentence.** If a sentence needs a semicolon, a parenthesis
  and a colon to hold together, it is three sentences.
- **Name the actor, in the active voice.** "`fetch` downloads the image," not
  "the image is downloaded."
- **Put the subject and verb early.** Don't make the reader hold a long
  opening clause before they learn what the sentence is about.
- **Keep sentences short.** Most should be under 25 words. Mix in longer ones
  when a thought needs them, but never stack clauses.
- **Avoid chains.** No chains of prepositions ("the closure of the group of the
  image of the build"), of `which`/`that` clauses, or of nested parentheses.
- **Use plain words.** "Use," not "utilize." "Needs," not "requires the
  presence of." Keep the correct technical term where one exists, and define
  it the first time it appears on a page.
- **No em dashes or en dashes as separators.** Use a period, a comma, or a
  colon.

## 3. Paragraphs, lists and tables

- **Short paragraphs.** Usually two to four sentences, each about one thing.
- **Lists for parallel items.** Three or more parallel things (steps, options,
  cases, rules) go in a list, not a sentence. Put a blank line before every
  list. Nested list items are indented four spaces past their parent marker.
- **Tables for comparisons.** When the reader will compare items across the
  same attributes (systems, engines, settings and their defaults), use a
  table. Keep table cells short; long explanations go in the text.
- **Examples early.** Show a concrete example (a spec fragment, a command and
  its output) close to the idea it illustrates, usually right after the first
  explanation. YAML is written in block style (decision 137).
- **Admonitions sparingly.** Use `!!! note` or `!!! warning` for something the
  reader must not miss, not for ordinary content.

## 4. Terms

- Define each Datadouble term (spec, source, stage, generation, image group,
  closure, standalone group, boundary, channel, copy, pool) in plain words the
  first time a page uses it, or link to where it is defined.
- Use one term for one thing, everywhere. Don't alternate synonyms for
  variety.
- Spell out what an abbreviation means the first time a page uses it.

## 5. Accuracy

Readability never costs correctness. When rewriting:

- Keep every fact, rule, default, limit, number, name, command, setting and
  link. If a sentence says something, the rewrite must still say it.
- If you find a statement that is ambiguous, say what it means precisely, and
  check the code if needed. If a statement is wrong, fix it and say so in your
  commit message.
- Keep links working. Prefer explicit anchors (`{#name}`) on headings that
  other pages link to, so a heading can be reworded without breaking links.
- Cite decisions by number ("decision 131"), linking to
  `decisions.md#decision-131` where it helps the reader.

## A worked example

Before:

> Subset planning decides which rows the extract reads. Package planning runs
> after it, once per group, and works out what the group's image needs to
> work: its closure. It starts from the group's tables, the objects the spec
> and the group declare in objects: (code outside Datadouble expects them), the
> objects that come with the group's tables (triggers on them, views over them
> alone, triggers on those views) and, for a standalone group, the objects its
> include patterns match, and follows, transitively and across schemas and
> databases:

The problem with it: the reader is not told why an image might "need"
anything, the key term arrives as an afterthought after a colon, and one
sentence lists four starting points, two qualifications and a traversal rule.

After:

> An image group's tables rarely work on their own. A trigger on `orders` may
> call a function in another schema. A foreign key may point at a table in
> another database. If the image leaves those out, it restores into a database
> that fails the first time someone inserts a row.
>
> So after Datadouble decides which rows to copy (subset planning), it works
> out, for each image group, everything that group's tables depend on. This is
> called package planning, and the set of things it finds is the group's
> **closure**.
>
> Package planning starts from:
>
> - the group's own tables;
> - the objects that come with those tables: triggers on them, views that read
>   only them, and triggers on those views;
> - objects the spec lists under `objects:`, because code outside Datadouble
>   expects them to be there;
> - for a standalone group, any objects its `include` patterns match.
>
> From there it follows each dependency to the next, across schemas and
> databases, until nothing new turns up.
