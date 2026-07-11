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
{{- end -}}

{{- define "agentic-ops.commonVolumes" -}}
- name: platform-config
  {{- include "agentic-ops.platformConfigVolume" . | nindent 2 }}
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
{{- if .Values.workflowRepo.existingClaim }}
- name: workflow-repo
  mountPath: {{ .Values.workflowRepo.mountPath }}
  readOnly: true
{{- end }}
{{- end -}}
