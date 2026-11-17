# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Jenkins for the Jenkins CI/CD conformance suite: pinned plugins, set up by
# Configuration as Code, with the docker command so jobs on the built-in
# node run the Datadouble image on the host's Docker daemon, as a
# self-hosted agent's would. Jobs check out repositories from local paths.
FROM jenkins/jenkins:2.580.2-lts-jdk21@sha256:74401de6a6f92e1afe1b02d1c8e3d0c33f263bfb5275dd8813f9150355f2367b
COPY --from=docker:29-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d \
    /usr/local/bin/docker /usr/local/bin/docker
# The suite's repositories belong to the user running the tests.
USER root
RUN git config --system --add safe.directory '*'
USER jenkins
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
# Each plugin's line names its URL in the Jenkins Maven repository.
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
COPY casc.yaml /usr/share/jenkins/ref/casc.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml \
    JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true"
