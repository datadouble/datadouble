// Copyright (c) 2026- Tight Line LLC
// SPDX-License-Identifier: MIT

/**
 * Runs one datadouble command in the Datadouble image, as the engine's user,
 * and returns its outputs (outcome, generation, error). A warning marks the
 * build unstable; a failure fails it. Jenkinsfiles written by
 * `datadouble ci init --system jenkins` call it:
 *
 *   datadouble(image: '...', user: '999:999', mounts: ['/srv/...'],
 *              environment: ['SHOP_SOURCE_DSN'], arguments: ['run', ...])
 *
 * Empty arguments, such as an unset --dry-run, are left out.
 */
def call(Map config) {
    writeFile(file: '.datadouble-run.sh', text: libraryResource('datadouble/run.sh'))
    List<String> arguments = config.arguments.findAll { it }
    int status
    withEnv([
        "DATADOUBLE_ARGUMENTS=${arguments.join('\n')}",
        "DATADOUBLE_IMAGE=${config.image}",
        "DATADOUBLE_USER=${config.user}",
        "DATADOUBLE_MOUNTS=${(config.mounts ?: []).join('\n')}",
        "DATADOUBLE_ENVIRONMENT=${(config.environment ?: []).join('\n')}",
    ]) {
        status = sh(script: 'bash .datadouble-run.sh', returnStatus: true)
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
