#!/bin/bash
# =============================================================================
# LAUNCHD INSTALLER — com.arjun.knowledge-ingestion
# =============================================================================
# Run once after cloning / pulling this branch to install the nightly schedule.
# Safe to re-run: unloads any existing job before reinstalling.
#
# Usage:
#   bash scripts/install_launchd.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.arjun.knowledge-ingestion.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.arjun.knowledge-ingestion.plist"
SECOND_PLIST_SRC="$SCRIPT_DIR/com.arjun.knowledge-ingestion-2am.plist"
SECOND_PLIST_DST="$HOME/Library/LaunchAgents/com.arjun.knowledge-ingestion-2am.plist"
RUNNER="$SCRIPT_DIR/run_nightly_ingestion.sh"
DOMAIN="gui/$(id -u)"
LABEL="com.arjun.knowledge-ingestion"
SECOND_LABEL="com.arjun.knowledge-ingestion-2am"

echo "Installing com.arjun.knowledge-ingestion..."

# Make runner executable
chmod +x "$RUNNER"

# Unload any existing job (ignore error if not loaded)
launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
launchctl bootout "$DOMAIN" "$SECOND_PLIST_DST" 2>/dev/null || true

# Copy and load both the primary run and its 02:00 recovery run.
cp "$PLIST_SRC" "$PLIST_DST"
cp "$SECOND_PLIST_SRC" "$SECOND_PLIST_DST"
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl bootstrap "$DOMAIN" "$SECOND_PLIST_DST"
launchctl enable "$DOMAIN/$LABEL"
launchctl enable "$DOMAIN/$SECOND_LABEL"

echo ""
echo "✓ Installed and loaded com.arjun.knowledge-ingestion"
echo "✓ Installed and loaded com.arjun.knowledge-ingestion-2am"
echo "  Schedule: nightly at 23:00 local time"
echo "  Runner:   $RUNNER"
echo "  Logs:     ingestion/logs/nightly/YYYY-MM-DD.log"
echo "  Verify:   launchctl print $DOMAIN/$LABEL"
echo "           launchctl print $DOMAIN/$SECOND_LABEL"
echo ""
echo "To uninstall:"
echo "  launchctl bootout $DOMAIN $PLIST_DST && launchctl bootout $DOMAIN $SECOND_PLIST_DST"
