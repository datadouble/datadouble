# datadouble-postgres

The PostgreSQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of the source with an exported snapshot,
builds working databases as throwaway PostgreSQL clusters in scratch space,
and packages golden images as a data directory (`physical`) or a `pg_dump`
archive (`logical`). Clusters, restores and `pg_dump` run in the official
PostgreSQL image of the source's exact version, through the spec's container
runtime; a source that uses extensions names an image that has them in
`source.image`.
