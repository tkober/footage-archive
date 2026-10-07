#!/usr/bin/env bash
#
# Footage Archive Opener — macOS setup script
#
# Installs a tiny helper that lets "Open in Photoshop" (and future apps) in
# Footage Archive hand off to a real desktop app. It registers the
# footage-archive:// URL scheme with a small AppleScript app
# ("~/Applications/Footage Archive Opener.app"), which in turn calls a plain
# shell script ("opener.sh") that validates the request and runs `open`.
# No admin rights are required — everything is installed under $HOME.
#
# Usage:
#   bash footage-archive-opener-macos.sh [--root /Volumes/footage] [--origin http://host:port] [--uninstall]
#
# Flags:
#   --root <path>     Local mount point of the footage share. Written to
#                      config.json on first install, or whenever given again.
#                      Defaults to /Volumes/footage.
#   --origin <origin> Base URL the Footage Archive app is served from, e.g.
#                      http://192.168.2.230:8050. Sets the Chrome policy
#                      AutoLaunchProtocolsFromOrigins so Chrome stops asking
#                      "Open Footage Archive Opener?" on every click.
#                      Chrome must be fully quit and restarted for this to
#                      take effect — check chrome://policy afterwards.
#   --uninstall        Remove the app, its support files and the Chrome
#                      policy (if set), then exit.
#
set -euo pipefail

APP="$HOME/Applications/Footage Archive Opener.app"
SUPPORT="$HOME/Library/Application Support/FootageArchiveOpener"
OPENER="$SUPPORT/opener.sh"
CONFIG="$SUPPORT/config.json"
BUNDLE_ID=de.tkober.footage-archive-opener
SCHEME=footage-archive
DEFAULT_ROOT=/Volumes/footage
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"

print_usage() {
  cat <<USAGE_EOF
Usage: $(basename "$0") [--root /Volumes/footage] [--origin http://host:port] [--uninstall]

Installs the Footage Archive Opener for this Mac. No admin rights needed.

Options:
  --root <path>      Local mount point of the footage share (default: $DEFAULT_ROOT).
                      Written to config.json on first install, or when given again.
  --origin <origin>  Base URL Footage Archive is served from (e.g. http://192.168.2.230:8050).
                      Sets the Chrome policy AutoLaunchProtocolsFromOrigins so Chrome no
                      longer asks "Open Footage Archive Opener?" on every click. Chrome
                      must be fully quit and restarted for this to take effect — check
                      chrome://policy afterwards.
  --uninstall         Remove the app, its support files and the Chrome policy, then exit.
  -h, --help          Show this help and exit.
USAGE_EOF
}

ROOT_ARG=""
ORIGIN_ARG=""
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      ROOT_ARG="${2:-}"
      shift 2
      ;;
    --origin)
      ORIGIN_ARG="${2:-}"
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

# Platform check comes after argument parsing so --help works on any OS
# (the test suite exercises --help on Linux).
if ! command -v osacompile >/dev/null 2>&1; then
  echo "This script only runs on macOS (osacompile not found)." >&2
  exit 1
fi

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -e "$APP" ]; then
    "$LSREGISTER" -u "$APP" || true
    rm -rf "$APP"
  fi
  rm -rf "$SUPPORT"
  if defaults read com.google.Chrome AutoLaunchProtocolsFromOrigins >/dev/null 2>&1; then
    defaults delete com.google.Chrome AutoLaunchProtocolsFromOrigins
    echo "Removed the Chrome policy AutoLaunchProtocolsFromOrigins — quit and restart Chrome for this to take effect."
  fi
  echo "Footage Archive Opener uninstalled."
  exit 0
fi

mkdir -p "$SUPPORT" "$HOME/Applications"

# --- opener.sh: the actual logic, as a quoted heredoc so nothing here expands ---
cat > "$OPENER" <<'OPENER_EOF'
#!/usr/bin/env bash
#
# Footage Archive Opener — logic.
# Called by the AppleScript app with the full footage-archive:// URL as $1.
# Writes error messages to stderr and exits 1 on failure; informational
# output (the "test" report) goes to stdout with exit 0. A successful
# "open" prints nothing. AppleScript's `do shell script` turns a non-zero
# exit into an error whose message is our stderr.
set -euo pipefail

CONFIG="$HOME/Library/Application Support/FootageArchiveOpener/config.json"

KNOWN_APPS="photoshop"

# Reads a dotted keypath (e.g. "root" or "apps.photoshop.bundleId") out of
# config.json. Uses plutil when available; otherwise falls back to a tiny
# python3 walker. The fallback exists only so this logic can be exercised
# by the test suite on Linux, where plutil doesn't exist.
cfg_get() {
  local key="$1"
  local value
  if command -v plutil >/dev/null 2>&1; then
    value="$(plutil -extract "$key" raw -o - "$CONFIG" 2>/dev/null)" || value=""
  else
    value="$(python3 - "$CONFIG" "$key" <<'PY_EOF'
import json
import sys

path, keypath = sys.argv[1], sys.argv[2]
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    print('')
    sys.exit(0)

current = data
for part in keypath.split('.'):
    if isinstance(current, dict) and part in current:
        current = current[part]
    else:
        print('')
        sys.exit(0)

if isinstance(current, dict):
    # Like `plutil -extract <key> raw`: a dictionary prints its keys, one
    # per line (an empty dictionary prints nothing).
    print('\n'.join(current.keys()))
elif isinstance(current, list):
    print(len(current))
elif current is None:
    print('')
else:
    print(current)
PY_EOF
)" || value=""
  fi
  printf '%s' "$value"
}

# True when <id> is a key of "apps" in config.json. `plutil -extract apps raw`
# prints a dictionary's keys one per line — an empty dict like
# "photoshop": {} would print nothing, so the check is on the key, not the
# value.
cfg_has_app() {
  cfg_get apps | grep -Fqx -- "$1"
}

app_label() {
  case "$1" in
    photoshop) printf '%s' "Photoshop" ;;
    *) printf '%s' "$1" ;;
  esac
}

app_bundle_id() {
  local id="$1"
  local override
  override="$(cfg_get "apps.$id.bundleId")"
  if [ -n "$override" ]; then
    printf '%s' "$override"
    return
  fi
  case "$id" in
    photoshop) printf '%s' "com.adobe.Photoshop" ;;
    *)
      echo "Unknown app: $id" >&2
      exit 1
      ;;
  esac
}

app_extensions() {
  local id="$1"
  case "$id" in
    photoshop) printf '%s' "jpg jpeg rw2 dng insp png tif tiff psd" ;;
    *)
      echo "Unknown app: $id" >&2
      exit 1
      ;;
  esac
}

# Percent-decodes a value produced by encodeURIComponent. "+" is literal
# there (not space), so we must not translate it, and we don't.
percent_decode() {
  local v="$1"
  printf '%b' "${v//%/\\x}"
}

cmd_test() {
  local root root_status
  root="$(cfg_get root)"
  if [ -n "$root" ] && [ -d "$root" ]; then
    root_status="✓"
  else
    root_status="✗ (not mounted?)"
  fi
  echo "Root: $root $root_status"

  local id bundle label app_path
  for id in $KNOWN_APPS; do
    if ! cfg_has_app "$id"; then
      continue
    fi
    label="$(app_label "$id")"
    bundle="$(app_bundle_id "$id")"
    app_path="$(osascript -e "POSIX path of (path to application id \"$bundle\")" 2>/dev/null)" || app_path=""
    if [ -n "$app_path" ]; then
      echo "$label: $app_path ✓"
    else
      echo "$label: not found ✗"
    fi
  done
}

cmd_open() {
  local app="$1"
  local path="$2"

  if [ -z "$app" ] || [ -z "$path" ]; then
    echo "app and path are required" >&2
    exit 1
  fi

  # Only apps explicitly listed in the local config may be used, regardless
  # of whether the app id is otherwise known — the URL itself only ever
  # carries an app id, never a program path or arguments.
  if ! cfg_has_app "$app"; then
    echo "Unknown app: $app" >&2
    exit 1
  fi

  case "$path" in
    /*)
      echo "Invalid path: $path" >&2
      exit 1
      ;;
  esac
  case "$path" in
    *'\'*)
      echo "Invalid path: $path" >&2
      exit 1
      ;;
  esac

  local segments seg
  IFS='/' read -ra segments <<< "$path"
  for seg in "${segments[@]}"; do
    if [ -z "$seg" ] || [ "$seg" = ".." ]; then
      echo "Invalid path: $path" >&2
      exit 1
    fi
  done

  local last ext extensions label found e
  last="${segments[${#segments[@]}-1]}"
  case "$last" in
    *.*) ext="${last##*.}" ;;
    *) ext="" ;;
  esac
  ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  label="$(app_label "$app")"
  extensions="$(app_extensions "$app")"
  found=0
  for e in $extensions; do
    if [ "$e" = "$ext" ]; then
      found=1
      break
    fi
  done
  if [ "$found" -ne 1 ]; then
    echo "File type .$ext is not supported by $label" >&2
    exit 1
  fi

  local root full
  root="$(cfg_get root)"
  if [ -z "$root" ]; then
    echo "No root configured" >&2
    exit 1
  fi
  full="$root/$path"

  if [ ! -d "$root" ]; then
    echo "Share not mounted: $root" >&2
    exit 1
  fi
  if [ ! -f "$full" ]; then
    echo "File not found: $full" >&2
    exit 1
  fi

  open -b "$(app_bundle_id "$app")" "$full"
}

if [ $# -lt 1 ]; then
  echo "Usage: opener.sh <footage-archive://...>" >&2
  exit 1
fi

raw_url="$1"
without_scheme="${raw_url#footage-archive://}"
case "$without_scheme" in
  *'?'*)
    action_part="${without_scheme%%\?*}"
    query_part="${without_scheme#*\?}"
    ;;
  *)
    action_part="$without_scheme"
    query_part=""
    ;;
esac
action="${action_part%/}"

app_param=""
path_param=""
if [ -n "$query_part" ]; then
  IFS='&' read -ra pairs <<< "$query_part"
  for pair in "${pairs[@]}"; do
    [ -z "$pair" ] && continue
    key="${pair%%=*}"
    val="${pair#*=}"
    decoded="$(percent_decode "$val")"
    case "$key" in
      app) app_param="$decoded" ;;
      path) path_param="$decoded" ;;
    esac
  done
fi

case "$action" in
  test)
    cmd_test
    ;;
  open)
    cmd_open "$app_param" "$path_param"
    ;;
  *)
    echo "Unknown action: $action" >&2
    exit 1
    ;;
esac
OPENER_EOF
chmod +x "$OPENER"

# --- config.json: written on first install, or whenever --root is given ---
if [ ! -f "$CONFIG" ] || [ -n "$ROOT_ARG" ]; then
  root_value="${ROOT_ARG:-$DEFAULT_ROOT}"
  cat > "$CONFIG" <<CONFIG_EOF
{
  "root": "$root_value",
  "apps": {
    "photoshop": {}
  }
}
CONFIG_EOF
fi

# --- the AppleScript app: a thin shell around opener.sh ---
rm -rf "$APP"
as_tmp="$(mktemp)"
cat > "$as_tmp" <<APPLESCRIPT_EOF
on open location theURL
	try
		set output to do shell script quoted form of "$OPENER" & " " & quoted form of theURL
		if output is not "" then
			display dialog output with title "Footage Archive Opener" buttons {"OK"} default button "OK" with icon note
		end if
	on error errMsg
		display alert "Footage Archive Opener" message errMsg as critical
	end try
end open location

on run
	display dialog "Footage Archive Opener is installed. Use “Open in Photoshop” in Footage Archive." with title "Footage Archive Opener" buttons {"OK"} default button "OK"
end run
APPLESCRIPT_EOF

osacompile -o "$APP" "$as_tmp"
rm -f "$as_tmp"

plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$APP/Contents/Info.plist"
plutil -replace LSUIElement -bool true "$APP/Contents/Info.plist"
plutil -replace CFBundleURLTypes -json "[{\"CFBundleURLName\": \"Footage Archive\", \"CFBundleURLSchemes\": [\"$SCHEME\"]}]" "$APP/Contents/Info.plist"

"$LSREGISTER" -f "$APP"

if [ -n "$ORIGIN_ARG" ]; then
  defaults write com.google.Chrome AutoLaunchProtocolsFromOrigins -array "{ protocol = \"$SCHEME\"; allowed_origins = (\"$ORIGIN_ARG\"); }"
  echo "Chrome policy set for origin $ORIGIN_ARG."
  echo "Quit Chrome completely and reopen it, then check chrome://policy."
fi

current_root="$(plutil -extract root raw -o - "$CONFIG" 2>/dev/null)" || current_root=""

echo ""
echo "Installed:"
echo "  App:    $APP"
echo "  Config: $CONFIG (root: $current_root)"
echo ""
echo "Now enable \"Opener is installed on this device\" in Settings → Open in and press Test."
