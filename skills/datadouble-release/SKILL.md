---
name: datadouble-release
description: Set up how Datadouble removes copies - when a request closes, by the scheduled sweep of expired copies, and by pool recycling. Use when copies pile up, a review environment's database must go away with it, or someone asks how copies are cleaned up.
---

# The release job

Follow `../RULES.md` throughout.

- **On-demand copies** go when their request closes (see
  `datadouble-provision` for how each system signals it), and the nightly
  sweep (`release --expired`) collects any whose teardown never ran, after
  `replicate.expires_hours`.
- **Pool copies** are recycled by `provision` itself: when another generation
  becomes active, unclaimed copies of other generations are replaced one for
  one, so the pool never empties. Claimed copies belong to their consumer
  until released with `datadouble release --owner NAME`.
- Copies that never finished are always removed.

Generate it with the other non-production jobs (`datadouble-fetch`). To see
what exists, `datadouble copies --spec <spec>` lists copies without their
credentials.
