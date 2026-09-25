{{- define "scilab.digest" -}}
{{- $digest := required (printf "%s.digest is required" .path) .digest -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $digest) -}}
{{- fail (printf "%s.digest must be 64 lowercase hexadecimal characters" .path) -}}
{{- end -}}
{{- $digest -}}
{{- end -}}

{{- define "scilab.image" -}}
{{- $repository := required (printf "%s.repository is required" .path) .image.repository -}}
{{- printf "%s@sha256:%s" $repository (include "scilab.digest" (dict "path" .path "digest" .image.digest)) -}}
{{- end -}}
