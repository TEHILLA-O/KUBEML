{{- define "kubeml.image" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end -}}
{{- define "kubeml.productionGuard" -}}
{{- if eq .root.Values.environment "production" -}}
{{- required (printf "%s.digest is required in production" .name) .image.digest -}}
{{- end -}}
{{- end -}}
