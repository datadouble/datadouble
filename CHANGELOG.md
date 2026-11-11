# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The `datadouble` package and command.** `datadouble version` reports the
  installed version, `--json` prints the JSON response instead of a summary,
  `--dry-run` prints the JSON request without running it, and `datadouble api`
  reads a JSON request from standard input. Every later command follows the
  same pattern, because the command line only builds JSON requests.
- **A container image, `ghcr.io/datadouble/datadouble`,** signed with cosign
  keyless signing and published with SBOM and build provenance attestations.
- **A documentation site** at <https://datadouble.tightlinesoftware.com/>,
  generated from the design documents.
- **Python support:** 3.13 or later, tested on 3.13, 3.14 and 3.15. The
  container image runs Python 3.15.
