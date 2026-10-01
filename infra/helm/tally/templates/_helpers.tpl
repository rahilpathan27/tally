{{- define "tally.name" -}}tally-{{ . }}{{- end -}}

{{- define "tally.labels" -}}
app.kubernetes.io/part-of: tally
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
tally.io/environment: {{ .root.Values.global.environment }}
{{- if .name }}
{{ include "tally.selector" .name }}
{{- end }}
{{- end -}}

{{- define "tally.selector" -}}
app.kubernetes.io/name: {{ include "tally.name" . }}
{{- end -}}

{{/* Image reference; digests win over tags so signatures can be verified. */}}
{{- define "tally.image" -}}
{{- $g := .root.Values.global.image -}}
{{- $repo := ternary $g.python $g.web (eq .kind "python") -}}
{{- $digest := ternary $g.pythonDigest $g.webDigest (eq .kind "python") -}}
{{- $prefix := ternary "" (printf "%s/" $g.registry) (empty $g.registry) -}}
{{- if $digest -}}
{{ $prefix }}{{ $repo }}@{{ $digest }}
{{- else -}}
{{ $prefix }}{{ $repo }}:{{ $g.tag }}
{{- end -}}
{{- end -}}

{{- define "tally.secretName" -}}tally-{{ . }}-secrets{{- end -}}

{{- define "tally.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop: [ALL]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Pod template shared by Deployments and Rollouts. Args: root, name, w (workload). */}}
{{- define "tally.podTemplate" -}}
{{- $root := .root -}}
{{- $w := .w -}}
{{- $name := .name -}}
metadata:
  labels:
    {{- include "tally.labels" (dict "root" $root "name" $name) | nindent 4 }}
  annotations:
    checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
spec:
  serviceAccountName: {{ include "tally.name" $name }}
  # IRSA does not need the API token: the EKS webhook injects its own projected STS token.
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: {{ $root.Values.podDefaults.terminationGracePeriodSeconds }}
  securityContext:
    runAsNonRoot: true
    runAsUser: {{ ternary 10001 65532 (eq $w.image "python") }}
    runAsGroup: {{ ternary 10001 65532 (eq $w.image "python") }}
    fsGroup: {{ ternary 10001 65532 (eq $w.image "python") }}
    seccompProfile:
      type: RuntimeDefault
  {{- if $root.Values.podDefaults.topologySpread }}
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: topology.kubernetes.io/zone
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels:
          {{- include "tally.selector" $name | nindent 10 }}
  {{- end }}
  containers:
    - name: {{ $name }}
      image: {{ include "tally.image" (dict "root" $root "kind" $w.image) }}
      imagePullPolicy: {{ $root.Values.global.image.pullPolicy }}
      {{- if $w.command }}
      command:
        {{- range $w.command }}
        - {{ . }}
        {{- end }}
        - --host=0.0.0.0
        - --port={{ $w.port }}
        - --timeout-graceful-shutdown=25
        - --no-server-header
        - --proxy-headers
        - --forwarded-allow-ips=*
      {{- end }}
      ports:
        - name: http
          containerPort: {{ $w.port }}
      envFrom:
        - configMapRef:
            name: tally-config
        {{- if $w.secretKeys }}
        - secretRef:
            name: {{ include "tally.secretName" $name }}
        {{- end }}
      env:
        - name: OTEL_SERVICE_NAME
          value: {{ include "tally.name" $name }}
        - name: OTEL_RESOURCE_ATTRIBUTES
          value: deployment.environment={{ $root.Values.global.environment }}
        - name: AWS_REGION
          value: {{ $root.Values.global.region }}
        {{- if eq $w.image "web" }}
        - name: PORT
          value: {{ $w.port | quote }}
        {{- end }}
        {{- range $peer := $w.calls }}
        {{- $peerW := index $root.Values.workloads $peer }}
        - name: {{ index $root.Values.peerEnv $peer }}
          value: http://{{ include "tally.name" $peer }}.{{ $root.Release.Namespace }}.svc:{{ $peerW.port }}
        {{- end }}
        {{- range $k, $v := $w.env }}
        - name: {{ $k }}
          value: {{ $v | quote }}
        {{- end }}
      livenessProbe:
        httpGet: {path: {{ $w.liveness | default "/health/live" }}, port: http}
        periodSeconds: 10
        failureThreshold: 3
      readinessProbe:
        httpGet: {path: {{ $w.readiness | default "/health/live" }}, port: http}
        periodSeconds: 5
        failureThreshold: 3
      startupProbe:
        httpGet: {path: {{ $w.liveness | default "/health/live" }}, port: http}
        periodSeconds: 2
        failureThreshold: 60
      resources:
        {{- toYaml $w.resources | nindent 8 }}
      securityContext:
        {{- include "tally.securityContext" . | nindent 8 }}
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: tmp
      emptyDir:
        sizeLimit: 256Mi
        medium: Memory
{{- end -}}

{{/* Egress to data stores (data subnets) and AWS endpoints (HTTPS). Args: root, stores. */}}
{{- define "tally.storeEgress" -}}
{{- $g := .root.Values.global -}}
{{- range $store := .stores }}
{{- $ports := index $.root.Values.storePorts $store }}
- to:
    {{- range (ternary $g.endpointCidrs $g.dataCidrs (eq $store "https")) }}
    - ipBlock: {cidr: {{ . }}}
    {{- end }}
  ports:
    {{- range $ports }}
    - {protocol: TCP, port: {{ . }}}
    {{- end }}
{{- end }}
{{- end -}}

{{- define "tally.backend" -}}
backend:
  service:
    name: {{ include "tally.name" .name }}
    port: {number: {{ (index .root.Values.workloads .name).port }}}
{{- end -}}
