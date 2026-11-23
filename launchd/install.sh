#!/bin/bash
# Runs Apple News Relay as a launchd agent for the current user: starts at login, restarts if it
# stops. Arguments are passed to `apple-news-relay serve`.
#   ./launchd/install.sh                                  localhost only
#   ./launchd/install.sh --host 0.0.0.0 --allow 127.0.0.1,192.168.1.0/24
#   ./launchd/install.sh --undo                           remove
# The first run asks macOS for Full Disk Access / Accessibility for python3: approve them.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

LABEL=io.github.vladislavekimtcov.apple-news-relay
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)
PYTHON=${PYTHON:-/usr/bin/python3}

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ ${1:-} == --undo ]]; then
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

ARGS=""
for a in "$@"; do ARGS+="    <string>${a//&/&amp;}</string>"$'\n'; done
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>apple_news_relay</string>
    <string>serve</string>
$ARGS  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONPATH</key><string>$PWD</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/apple-news-relay.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/apple-news-relay.log</string>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 2
launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|last exit code|runs)' || true
echo "installed $LABEL (log: ~/Library/Logs/apple-news-relay.log)"
