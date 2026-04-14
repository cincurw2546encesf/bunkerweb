#!/bin/bash
#
# Supervisor child wrapper for BunkerWeb all-in-one Python services.
#
# Responsibilities:
#   1. Export LOG_FILE_PATH with "user-env-wins" semantics so a `docker run -e
#      LOG_FILE_PATH=/custom/path` is honored but the AIO supplies a sensible default
#      per-service when the user didn't set one.
#   2. Prefix each line of the service's merged stdout+stderr with a "[SERVICE] "
#      tag so `docker logs bunkerweb-aio` stays readable when multiple services
#      share a single stream.
#   3. Honor HIDE_SERVICE_LOGS: when the service matches the configured list,
#      redirect its output to /dev/null so it doesn't reach `docker logs`.
#
# Intentionally does NOT tee to an on-disk file. Python services own their own
# bounded on-disk logging via BWLogger's RotatingFileHandler (configured via
# LOG_TYPES, LOG_FILE_PATH, LOG_FILE_MAX_BYTES, LOG_FILE_BACKUP_COUNT).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/logging-utils.sh"

service_key="$1"
prefix="$2"
default_log_file_path="$3"
shift 3

# User's explicit LOG_FILE_PATH (via `docker run -e ...`) wins; otherwise use the
# per-service default supplied by the supervisor INI.
export LOG_FILE_PATH="${LOG_FILE_PATH:-${default_log_file_path}}"

if hide_service_logs_match "$service_key"; then
	exec "$@" >/dev/null 2>&1
else
	# Strip C0 control characters (except tab `\011` and newline `\012`) plus DEL so an
	# adversarial log payload can't inject ANSI/CSI/OSC escape sequences into
	# `docker logs` output and spoof other services' lines.
	exec "$@" 2>&1 | tr -d '\000-\010\013-\037\177' | while IFS= read -r line; do printf '%s%s\n' "${prefix}" "${line}"; done
fi
