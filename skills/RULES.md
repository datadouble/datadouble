# Rules every Datadouble skill follows

These hold for every skill in this plugin. A skill that cannot follow them
stops and tells the person why.

1. **Check the version first.** Run `datadouble version`. These skills are
   released with Datadouble; if its version is not the plugin's (in
   `.claude-plugin/plugin.json`), say so and stop. Never install or upgrade
   Datadouble without being asked.
2. **Datadouble writes the configuration.** CI/CD configuration comes only
   from `datadouble ci init`; never write or edit workflow, pipeline,
   Jenkinsfile or Application files by hand. To change them, change the spec
   or the `ci init` options and run it again. Spec changes you make for a
   person are checked with `datadouble plan` before you finish.
3. **Inspect before you ask.** Look for the CI/CD system in use
   (`.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`, Argo CD
   Applications), existing specs and existing Datadouble jobs. Ask only for
   what you cannot find.
4. **Never weaken safety settings.** Do not lower `strict`, add excludes,
   add `source.omit` entries, drop verification or anonymization rules, or
   pass `--no-strict` to make a run pass. Report the failure, what it means,
   and the person's options.
5. **Production stays in production.** Never generate `build` or `promote`
   for a vendor-hosted runner (GitHub's `ubuntu-*`, GitLab's `saas-*`), never
   give a non-production job a source's or production store's credentials,
   and never mark a runner inside the boundary yourself: that is the
   operator's decision (`DATADOUBLE_BOUNDARY=inside`, decision 98).
6. **Never handle secrets or production data.** Do not ask for, read, print
   or store passwords, keys or tokens, and do not connect to a production
   system. Specs name secrets by reference (`{env: NAME}`); `ci init` lists
   the variables to define, and you tell the person where to define them.
