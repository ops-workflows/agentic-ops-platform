{{- define "agentic-ops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentic-ops.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "agentic-ops.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "agentic-ops.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agentic-ops.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "agentic-ops.platformConfigVolume" -}}
{{- if .Values.platformConfig.existingSecret }}
secret:
  secretName: {{ .Values.platformConfig.existingSecret }}
{{- else if .Values.platformConfig.existingConfigMap }}
configMap:
  name: {{ .Values.platformConfig.existingConfigMap }}
{{- else }}
configMap:
  name: {{ include "agentic-ops.fullname" . }}-platform-config
{{- end }}
{{- end -}}

{{- define "agentic-ops.commonEnv" -}}
- name: PLATFORM_CONFIG_FILE
  value: /app/config/platform-config.yaml
- name: WORKFLOW_ROOT
  value: {{ .Values.workflowRepo.workflowRoot | quote }}
- name: WORKFLOW_REPO_PATHS
  value: {{ .Values.workflowRepo.workflowRepoPaths | quote }}
- name: WORKFLOW_REPO_LOCAL_PATH
  value: {{ .Values.workflowRepo.syncPath | quote }}
{{- if .Values.infrastructure.postgres.enabled }}
- name: PG_HOST
  value: {{ include "agentic-ops.postgresName" . | quote }}
- name: PG_PORT
  value: "5432"
- name: PG_DB
  value: {{ .Values.infrastructure.postgres.database | quote }}
- name: PG_USER
  value: {{ .Values.infrastructure.postgres.user | quote }}
{{- end }}
{{- if .Values.infrastructure.objectStore.enabled }}
- name: OBJECT_STORE_PROVIDER
  value: s3
- name: OBJECT_STORE_ENDPOINT
  value: {{ printf "%s:9000" (include "agentic-ops.objectStoreName" .) | quote }}
- name: OBJECT_STORE_ACCESS_KEY
  value: {{ .Values.infrastructure.objectStore.accessKey | quote }}
{{- end }}
{{- if .Values.infrastructure.hindsight.enabled }}
- name: HINDSIGHT_URL
  value: {{ printf "http://%s:8888" (include "agentic-ops.hindsightName" .) | quote }}
{{- end }}
{{- range $key, $value := .Values.platformEnv }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{- define "agentic-ops.postgresName" -}}
{{- printf "%s-postgres" (include "agentic-ops.fullname" .) -}}
{{- end -}}

{{- define "agentic-ops.objectStoreName" -}}
{{- printf "%s-object-store" (include "agentic-ops.fullname" .) -}}
{{- end -}}

{{- define "agentic-ops.hindsightName" -}}
{{- printf "%s-hindsight" (include "agentic-ops.fullname" .) -}}
{{- end -}}

{{- define "agentic-ops.bootstrapEnvFrom" -}}
{{- if .Values.bootstrap.existingSecret }}
- secretRef:
    name: {{ .Values.bootstrap.existingSecret }}
{{- end }}
{{- end -}}

{{- define "agentic-ops.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "agentic-ops.commonVolumes" -}}
- name: platform-config
  {{- include "agentic-ops.platformConfigVolume" . | nindent 2 }}
- name: workflow-repo-cache
  emptyDir: {}
- name: release-cache
  emptyDir: {}
{{- if .Values.workflowRepo.existingClaim }}
- name: workflow-repo
  persistentVolumeClaim:
    claimName: {{ .Values.workflowRepo.existingClaim }}
{{- end }}
{{- end -}}

{{- define "agentic-ops.commonVolumeMounts" -}}
- name: platform-config
  mountPath: /app/config
  readOnly: true
- name: workflow-repo-cache
  mountPath: {{ .Values.workflowRepo.syncPath }}
- name: release-cache
  mountPath: {{ .Values.runtimeBundles.root }}
{{- if .Values.workflowRepo.existingClaim }}
- name: workflow-repo
  mountPath: {{ .Values.workflowRepo.mountPath }}
  readOnly: true
{{- end }}
{{- end -}}
