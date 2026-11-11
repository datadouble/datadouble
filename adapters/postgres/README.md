# datadouble-postgres

The PostgreSQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of the source with an exported snapshot,
builds working databases as throwaway local PostgreSQL clusters in scratch
space, and packages golden images as a data directory (`physical`) or a
`pg_dump` archive (`logical`). The machine running it needs the PostgreSQL
server binaries of the version images are built for, and any extensions the
source uses.
