{{/* Copyright (c) 2026- Tight Line LLC */}}
{{/* SPDX-License-Identifier: MIT */}}

{{/* The side's name: its CronJobs, service account and spec are named after it. */}}
{{- define "datadouble.name" -}}
{{- .Values.name | default (printf "datadouble-%s" .Release.Name) | trunc 52 | trimSuffix "-" -}}
{{- end -}}

{{/* One step's container: datadouble with its arguments, as the engine's user. */}}
{{- define "datadouble.container" -}}
{{- $root := .root -}}
- name: {{ .name }}
  image: {{ $root.Values.image }}
  workingDir: /workspace
  args:
    - --ci
    - argocd
    {{- with .step.job }}
    - --ci-job
    - {{ . | quote }}
    {{- end }}
    {{- range .step.arguments }}
    {{- if eq . "$(DATADOUBLE_DRY_RUN)" }}
    {{- if $root.Values.dryRun }}
    - --dry-run
    {{- end }}
    {{- else }}
    - {{ . | quote }}
    {{- end }}
    {{- end }}
  env:
    - name: HOME
      value: /tmp
    - name: DATADOUBLE_RUN
      valueFrom:
        fieldRef: {fieldPath: "metadata.labels['batch.kubernetes.io/job-name']"}
    - name: DATADOUBLE_OWNER
      value: {{ $root.Values.owner | quote }}
    - name: DATADOUBLE_GENERATION
      value: {{ $root.Values.generation | quote }}
    {{- range .step.environment }}
    - name: {{ . }}
      valueFrom:
        secretKeyRef: {name: {{ $root.Values.secret | quote }}, key: {{ . | quote }}}
    {{- end }}
  envFrom:
    # The production boundary mark, which the cluster's operator sets in
    # production namespaces only (decision 98).
    - configMapRef: {name: datadouble-boundary, optional: true}
  securityContext:
    runAsUser: {{ $root.Values.user.uid }}
    runAsGroup: {{ $root.Values.user.gid }}
    runAsNonRoot: true
    allowPrivilegeEscalation: false
  terminationMessagePolicy: File
  volumeMounts:
    {{- if $root.Values.spec.configMap }}
    - {name: spec, mountPath: {{ dir $root.Values.spec.path | quote }}, readOnly: true}
    {{- end }}
    {{- range $index, $file := keys $root.Values.spec.files | sortAlpha }}
    - {name: spec, mountPath: {{ printf "/workspace/%s/%s" (dir $root.Values.spec.path) $file | quote }}, subPath: {{ printf "spec-%d" $index }}}
    {{- end }}
    {{- if $root.Values.spec.lock }}
    {{- $path := $root.Values.spec.path }}
    - {name: spec, mountPath: {{ printf "/workspace/%s.lock" (trimSuffix (ext $path) $path) | quote }}, subPath: lock}
    {{- end }}
    - {name: claim, mountPath: {{ $root.Values.claim.mount | quote }}}
    {{- if .artifacts }}
    - {name: artifacts, mountPath: {{ printf "/workspace/%s" .artifacts | quote }}}
    {{- end }}
{{- end -}}

{{/* The Job's pod failure rules: a failed follower's step (promote after build,
provision after fetch) fails the Job, rather than running the pod again,
which would run its leader again: a finished build built, and notified, twice. */}}
{{- define "datadouble.followers" -}}
{{- $leader := (first .steps).job -}}
{{- range $index, $step := .steps }}
{{- if ne $step.job $leader }}
- action: FailJob
  onExitCodes:
    containerName: {{ printf "step-%d" (add $index 1) }}
    operator: NotIn
    values: [0]
{{- end }}
{{- end }}
{{- end -}}

{{/* A job's pod: earlier steps as init containers, the last as the container. */}}
{{- define "datadouble.pod" -}}
{{- $root := .root -}}
{{- $job := .job -}}
{{- $count := len $job.steps -}}
metadata:
  labels:
    app.kubernetes.io/name: datadouble
    app.kubernetes.io/instance: {{ $root.Release.Name }}
    datadouble.tightlinesoftware.com/job: {{ $job.name }}
spec:
  serviceAccountName: {{ include "datadouble.name" $root }}
  restartPolicy: Never
  securityContext:
    fsGroup: {{ $root.Values.user.gid }}
    # Only when the claim's root does not already have the group: a recursive
    # change on every start would walk a terabyte-scale work directory.
    fsGroupChangePolicy: OnRootMismatch
  {{- if gt $count 1 }}
  initContainers:
    {{- range $index, $step := $job.steps }}
    {{- if lt (add $index 1) $count }}
    {{- include "datadouble.container" (dict "root" $root "step" $step "artifacts" $job.artifacts "name" (printf "step-%d" (add $index 1))) | nindent 4 }}
    {{- end }}
    {{- end }}
  {{- end }}
  containers:
    {{- include "datadouble.container" (dict "root" $root "step" (last $job.steps) "artifacts" $job.artifacts "name" (printf "step-%d" $count)) | nindent 4 }}
  volumes:
    - name: spec
      configMap: {name: {{ $root.Values.spec.configMap | default (include "datadouble.name" $root) | quote }}}
    - name: claim
      persistentVolumeClaim: {claimName: {{ $root.Values.claim.name | quote }}}
    {{- if $job.artifacts }}
    # Files the job writes for review (the lock job's new lock and changes),
    # printed to its log; Kubernetes keeps no job artifacts.
    - name: artifacts
      emptyDir: {}
    {{- end }}
{{- end -}}
