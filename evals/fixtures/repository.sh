#!/usr/bin/env bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# A team's repository for the skill scenarios: a CI configuration and a
# Datadouble spec, with secrets named by reference only. The first
# argument picks the CI system's files: github (default), gitlab or none.
set -euo pipefail
system=${1:-github}
# The keyless signer the spec trusts is the CI system's own.
case $system in
    gitlab)
        identity=https://gitlab.example.com/example/shop//.gitlab-ci.yml@refs/heads/main
        issuer=https://gitlab.example.com ;;
    *)
        identity=https://github.com/example/shop/.github/workflows/datadouble-shop-build.yml@refs/heads/main
        issuer=https://token.actions.githubusercontent.com ;;
esac
mkdir -p datadouble
cat > datadouble/shop.yml <<SPEC
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_SOURCE_DSN}}
runtime: {docker: {}}
subset:
  seeds: {customer: "id <= 50"}
anonymize:
  customer:
    email: {faker: email, unique: true}
store: [{filesystem: {path: /srv/datadouble/store}}]
signing: {cosign: {keyless: true}}
trust:
  cosign:
    identity: $identity
    issuer: $issuer
promote: {to: [{filesystem: {path: /srv/datadouble/export}}]}
fetch:
  from: {filesystem: {path: /srv/datadouble/export}}
  staging: /srv/datadouble/staging
replicate:
  using: {docker-volume: {}}
  mode: on-demand
  size: 1
SPEC
case $system in
    github)
        mkdir -p .github/workflows
        printf 'name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n' \
            > .github/workflows/ci.yml ;;
    gitlab)
        printf 'test:\n  script: [make test]\n' > .gitlab-ci.yml ;;
esac
mkdir -p sample-uploads/avatars sample-uploads/documents
printf 'jpeg\n' > sample-uploads/avatars/1.jpg
printf 'jpeg\n' > sample-uploads/avatars/2.jpg
# Long unchanged; POSIX -t, which macOS and GNU touch both take.
touch -t 202001010000 sample-uploads/avatars/2.jpg
printf 'pdf\n' > sample-uploads/documents/1.pdf
git init --quiet
git add --all
git -c user.name=Scenario -c user.email=scenario@example.com commit --quiet --message "A shop"
