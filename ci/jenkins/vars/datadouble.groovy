// Copyright (c) 2026- Tight Line LLC
// SPDX-License-Identifier: MIT

/**
 * Runs one datadouble command in the Datadouble image, as the engine's user,
 * and returns its outputs (outcome, generation, error). A warning marks the
 * build unstable; a failure fails it. Jenkinsfiles written by
 * `datadouble ci init --system jenkins` call it:
 *
 *   datadouble(job: 'build', image: '...', user: '999:999', mounts: ['/srv/...'],
 *              environment: ['SHOP_SOURCE_DSN'], arguments: ['run', ...])
 *
 * Empty arguments, such as an unset --dry-run, are left out. `job` names the
 * generated job, so the command posts the spec's notify webhooks itself.
 * `artifacts` names a workspace directory the command writes files into for
 * review, such as the lock job's new lock and its changes: it is emptied
 * first, made writable by the engine's user, and archived as the build's
 * artifacts.
 */
def call(Map config) {
    writeFile(file: '.datadouble-run.sh', text: libraryResource('datadouble/run.sh'))
    List<String> arguments = config.arguments.findAll { it }
    int status
    withEnv([
        "DATADOUBLE_ARGUMENTS=${arguments.join('\n')}",
        "DATADOUBLE_CI_JOB=${config.job ?: ''}",
        "DATADOUBLE_IMAGE=${config.image}",
        "DATADOUBLE_USER=${config.user}",
        "DATADOUBLE_MOUNTS=${(config.mounts ?: []).join('\n')}",
        "DATADOUBLE_ENVIRONMENT=${(config.environment ?: []).join('\n')}",
        "DATADOUBLE_ARTIFACTS=${config.artifacts ?: ''}",
    ]) {
        status = sh(script: 'bash .datadouble-run.sh', returnStatus: true)
    }
    if (config.artifacts) {
        archiveArtifacts(artifacts: "${config.artifacts}/**", allowEmptyArchive: true)
    }
    Map<String, String> outputs = [:]
    if (fileExists('.datadouble-outputs')) {
        for (String line : readFile('.datadouble-outputs').readLines()) {
            int split = line.indexOf('=')
            if (split > 0) {
                outputs[line.substring(0, split)] = line.substring(split + 1)
            }
        }
    }
    if (outputs.generation) {
        currentBuild.description = "generation ${outputs.generation}"
    }
    if (status == 3) {
        unstable("datadouble ${arguments[0]} finished with warnings")
    } else if (status != 0) {
        error("datadouble ${arguments[0]} failed (exit status ${status})")
    }
    return outputs
}
