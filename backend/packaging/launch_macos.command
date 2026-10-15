#!/bin/zsh
# User-facing launcher for the PyInstaller onedir distribution.

set -u

quote_applescript_string() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

quote_shell_arg() {
  printf "'%s'" "${1//\'/\'\\\'\'}"
}

bundle_dir="${0:A:h}"
controller="$bundle_dir/SonarHeadUnitController"
if [[ ! -x "$controller" && "$bundle_dir:t" == "packaging" ]]; then
  built_bundle_dir="$bundle_dir/../../build/backend/SonarHeadUnit"
  built_controller="$built_bundle_dir/SonarHeadUnitController"
  if [[ -x "$built_controller" ]]; then
    bundle_dir="${built_bundle_dir:A}"
    controller="$built_controller"
  fi
fi
data_dir="$HOME/Library/Application Support/SonarHeadUnit"
recordings_dir="$data_dir/recordings"
log_dir="$data_dir/logs"
controller_url="http://127.0.0.1:8765"

if [[ ! -x "$controller" ]]; then
  osascript -e "display dialog \"Sonar Head Unit is incomplete. Open the copy in build/backend/SonarHeadUnit, or rebuild the application. Checked: $bundle_dir\" buttons {\"OK\"} default button \"OK\" with icon stop" >/dev/null 2>&1 || true
  exit 1
fi

mkdir -p "$recordings_dir" "$log_dir"
# Start every launched session with a fresh log without prompting. The
# controller command below appends its output after this explicit truncation.
: > "$log_dir/controller.log"
echo "Requesting administrator permission from macOS..."
echo "Open this link if the browser does not appear:"
echo "$controller_url"
echo "Log file: $log_dir/controller.log"

cd "$bundle_dir" || exit 1
controller_command="/usr/bin/id -u > $(quote_shell_arg "$log_dir/controller.uid"); \
HOME=$(quote_shell_arg "$HOME") SUDO_UID=$(id -u) SUDO_GID=$(id -g) PYTHONUNBUFFERED=1 \
$(quote_shell_arg "$controller") --recordings-root $(quote_shell_arg "$recordings_dir") \
>> $(quote_shell_arg "$log_dir/controller.log") 2>&1"

# Run the controller in the authorized shell rather than detaching it. macOS
# ties the authorization to this shell; foreground execution ensures its child
# engine retains root privileges for ifconfig and packet capture.
(sleep 2; open "$controller_url" >/dev/null 2>&1) &
if ! osascript - "$controller_command" <<'APPLESCRIPT'
on run argv
  do shell script (item 1 of argv) with administrator privileges
end run
APPLESCRIPT
then
  osascript -e 'display dialog "Sonar Head Unit stopped during startup. See ~/Library/Application Support/SonarHeadUnit/logs/controller.log for the specific error." buttons {"OK"} default button "OK" with icon caution' >/dev/null 2>&1 || true
  exit 1
fi
