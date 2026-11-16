# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# A GitLab Runner for the GitLab CI/CD conformance suite: its shell executor
# runs jobs as a self-hosted runner's would, with the docker command for the
# Datadouble image and the host's Docker daemon through its socket.
FROM gitlab/gitlab-runner:ubuntu-v19.5.2@sha256:6d4cdfe23443fa5463bffa65cba9074a4517f68784756b69bcc362ca86f3d4c6
COPY --from=docker:29-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d \
    /usr/local/bin/docker /usr/local/bin/docker
