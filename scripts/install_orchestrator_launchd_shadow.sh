#!/bin/bash
# =============================================================================
# SCRIPT NAME: scripts/install_orchestrator_launchd_shadow.sh
# =============================================================================
#
# DESCRIPTION:
# Install / status / uninstall ONLY the new Phase 4 shadow-validation sidecar
# LaunchAgent (com.arjun.pks-nightly-orchestrator.shadow). It never unloads,
# bootouts, disables, edits, or replaces the existing production ingestion jobs
# (com.arjun.knowledge-ingestion, com.arjun.knowledge-ingestion-2am).
#
# INPUT FILES:
# - scripts/com.arjun.pks-nightly-orchestrator.shadow.plist (the LaunchAgent)
# - scripts/run_orchestrator_launchd.sh (made executable)
#
# OUTPUT FILES:
# - ~/Library/LaunchAgents/com.arjun.pks-nightly-orchestrator.shadow.plist (copy)
#
# VERSION: 1.0 | LAST UPDATED: 2026-06-17 | AUTHOR: Claude Code for Arjun
#
# USAGE:
#   bash scripts/install_orchestrator_launchd_shadow.sh install
#   bash scripts/install_orchestrator_launchd_shadow.sh status
#   bash scripts/install_orchestrator_launchd_shadow.sh uninstall
# =============================================================================
set -uo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
LABEL="com.arjun.pks-nightly-orchestrator.shadow"
SRC_PLIST="$REPO/scripts/$LABEL.plist"
DEST_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$REPO/scripts/run_orchestrator_launchd.sh"
UID_NUM="$(id -u)"
OLD_LABELS=("com.arjun.knowledge-ingestion" "com.arjun.knowledge-ingestion-2am")

print_status() {
  echo "=== new sidecar: $LABEL ==="
  launchctl print "gui/${UID_NUM}/${LABEL}" 2>/dev/null | grep -E "state =|program =|run interval|last exit code" || echo "  (not loaded)"
  echo "=== old production jobs (must remain untouched) ==="
  for L in "${OLD_LABELS[@]}"; do
    if launchctl print "gui/${UID_NUM}/${L}" >/dev/null 2>&1; then
      echo "  $L: LOADED"
    else
      echo "  $L: NOT loaded"
    fi
  done
}

case "${1:-status}" in
  install)
    echo "Linting plist ..."
    plutil -lint "$SRC_PLIST" || { echo "plutil -lint FAILED"; exit 1; }
    chmod +x "$WRAPPER"
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC_PLIST" "$DEST_PLIST"
    echo "Copied -> $DEST_PLIST"
    # Modern bootstrap; tolerate an already-bootstrapped state, then enable.
    launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_NUM}" "$DEST_PLIST" 2>&1 || {
      echo "bootstrap reported an issue; checking load state ..."; }
    launchctl enable "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
    echo
    print_status
    ;;
  status)
    print_status
    ;;
  uninstall)
    echo "Removing ONLY $LABEL (old jobs untouched) ..."
    launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
    launchctl disable "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
    rm -f "$DEST_PLIST"
    echo "Removed $DEST_PLIST"
    echo
    print_status
    ;;
  *)
    echo "usage: $0 {install|status|uninstall}"; exit 2 ;;
esac
