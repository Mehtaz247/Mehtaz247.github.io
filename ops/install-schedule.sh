#!/usr/bin/env bash
# Installs (or reinstalls) the launchd job that runs Overhead autonomously.
#
# Twice a week: Monday and Thursday at 09:07 local time. That cadence supports
# the weekly publishing target in strategy.json with one preparation run and one
# writing run, which produces better posts than trying to design an experiment,
# run it, and write it up in a single session.
#
# The odd minute is deliberate -- nothing else on the machine fires at :07.
#
# Usage: ops/install-schedule.sh [--uninstall] [--status]

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.overhead.blog.cycle"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

case "${1:-}" in
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled $LABEL"
    exit 0
    ;;
  --status)
    echo "plist:   $([ -f "$PLIST" ] && echo present || echo absent)"
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E '^\s+(state|last exit code|runs)' || echo "not loaded"
    echo
    echo "recent cycles:"
    ls -1t "$REPO/ops/logs"/cycle-*.log 2>/dev/null | head -5 || echo "  none yet"
    exit 0
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/ops/logs"

cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$REPO/ops/cycle.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$REPO</string>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>7</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>7</integer></dict>
  </array>

  <!-- The laptop is often asleep at 09:07. Without this the cycle is simply
       skipped; with it, launchd runs the job once the machine wakes. -->
  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>$REPO/ops/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO/ops/logs/launchd.err.log</string>

  <key>ProcessType</key>
  <string>Background</string>

  <!-- A cycle involves model calls and benchmark runs; it needs room. -->
  <key>ExitTimeOut</key>
  <integer>3600</integer>
</dict>
</plist>
PLIST_END

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $LABEL"
echo "  schedule: Mon and Thu at 09:07 local"
echo "  script:   $REPO/ops/cycle.sh"
echo "  logs:     $REPO/ops/logs/"
echo
echo "check with: ops/install-schedule.sh --status"
echo "remove with: ops/install-schedule.sh --uninstall"
