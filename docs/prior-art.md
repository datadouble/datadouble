# Prior art

Surveyed November 2026 from public sources, not hands-on trials. Corrections
welcome.

## Summary

No open-source tool covers more than a few of the target systems. Commercial
test-data platforms come closest, and even they leave gaps (nobody covers Redis).
Datadouble's niche is an open, pluggable toolkit across the whole estate.

| Tool | Licence / status | Systems | Subset | Anonymize | Notes |
|---|---|---|---|---|---|
| [Greenmask](https://github.com/GreenmaskIO/greenmask) | Open source, active | PostgreSQL (stable), MySQL (beta) | Yes, cycles and polymorphic references (PostgreSQL) | Yes | Best open-source design to study |
| [Jailer](https://github.com/Wisser/Jailer) | Apache-2.0, active | Any JDBC database | Yes, its core strength | No | Mature FK traversal and GUI; Datadouble reads its models natively |
| [Neosync](https://github.com/nucleuscloud/neosync) | Archived Aug 2025 | PostgreSQL, MySQL | Yes | Yes | Acquired; repository no longer maintained |
| [PostgreSQL Anonymizer](https://www.postgresql.org/about/news/postgresql-anonymizer-20-better-faster-safer-2993) | Open source | PostgreSQL | No | Yes | Runs inside PostgreSQL |
| [SFDMU](https://www.github.com/forcedotcom/SFDX-Data-Move-Utility) | BSD-3, active | Salesforce | Related-record selection | Yes | Candidate to wrap for the Salesforce adapter |
| [DBLab Engine](https://github.com/postgres-ai/database-lab-engine) | Open source | PostgreSQL | — | — | Copy-on-write thin clones; candidate replication backend |
| Small MongoDB anonymizers | Open source | MongoDB | No | Faker-based | Hobby-scale |
| [Tonic Structural](https://www.tonic.ai/integrations) | Commercial | PostgreSQL, MySQL, SQL Server, Oracle, MongoDB, Db2; Salesforce on request | Yes, including MongoDB | Yes | Closest to the target coverage |
| [K2view](https://www.k2view.com/blog/test-data-management-salesforce/) | Commercial | Claims Salesforce, MongoDB, Oracle and more | Entity-based | Yes | Enterprise platform |
| [Redgate Test Data Manager](https://documentation.red-gate.com/tdm/command-line-interface-cli/subsetting) | Commercial | SQL Server, PostgreSQL, MySQL, Oracle | Yes | Yes | Relational only; also clones |
| Delphix, Synthesized | Commercial | Mostly relational | Yes | Yes | No MongoDB, Redis or Salesforce confirmed |

## What to reuse rather than rebuild

- **Wrap** SFDMU (Salesforce), native dump tools, and DBLab Engine (PostgreSQL
  thin clones) as adapters where they fit.
- **Study** Greenmask's subset model and Jailer's traversal before finalizing the
  planner.
- **Read** Jailer models natively instead of inventing a competing GUI.

## Lesson from Neosync

A well-funded, MIT-licensed project in this space still stopped. That argues for
a small core with community-maintainable adapters, not a platform that depends
on a hosted business.
