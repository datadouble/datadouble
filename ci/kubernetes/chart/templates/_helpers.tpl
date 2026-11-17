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
    - {name: spec, mountPath: {{ printf "/workspace/%s" $root.Values.spec.path | quote }}, subPath: spec}
    {{- if $root.Values.spec.lock }}
    - {name: spec, mountPath: {{ printf "/workspace/%s/catalog.lock" (dir $root.Values.spec.path) | quote }}, subPath: lock}
    {{- end }}
    - {name: claim, mountPath: {{ $root.Values.claim.mount | quote }}}
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
  {{- if gt $count 1 }}
  initContainers:
    {{- range $index, $step := $job.steps }}
    {{- if lt (add $index 1) $count }}
    {{- include "datadouble.container" (dict "root" $root "step" $step "name" (printf "step-%d" (add $index 1))) | nindent 4 }}
    {{- end }}
    {{- end }}
  {{- end }}
  containers:
    {{- include "datadouble.container" (dict "root" $root "step" (last $job.steps) "name" (printf "step-%d" $count)) | nindent 4 }}
  volumes:
    - name: spec
      configMap: {name: {{ include "datadouble.name" $root }}}
    - name: claim
      persistentVolumeClaim: {claimName: {{ $root.Values.claim.name | quote }}}
{{- end -}}
