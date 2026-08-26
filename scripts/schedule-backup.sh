#!/usr/bin/env bash
#
# Make the backup happen without anybody remembering to run it.
#
# `backup.sh` has been correct and unscheduled for weeks, which is the same as absent on the
# morning it is needed: on 25 August a Docker reset discarded the database volume, and what
# saved the installation was a backup somebody had taken by hand four days earlier. That is
# not a recovery plan, it is luck with a shell script attached.
#
#   ./scripts/schedule-backup.sh install [destination]   # daily at 03:00
#   ./scripts/schedule-backup.sh status
#   ./scripts/schedule-backup.sh remove
#
# **The destination is still yours to choose, and it should not be this disk.** The default is
# `./backups`, which is what `backup.sh` warns about every run — scheduling a copy onto the
# disk it protects means never forgetting to make a backup that a disk failure takes with it.
# Pass a path on other hardware and this schedules that instead.
#
# macOS uses `launchd`; anything else gets the crontab line printed for review rather than
# installed, because a script that edits a server's crontab unasked is a script that has
# opinions about somebody else's machine.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="enterprise.zenith.backup"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
ACTION="${1:-status}"
DESTINATION="${2:-${HERE}/backups}"

# 03:00, and the hour matters more than it looks. `pg_dump` and the embedder compete for the
# same cores, and F11 measured ingestion quadrupling query latency — a backup at nine in the
# morning is a backup that makes the product look slow.
HOUR=3
MINUTE=0

case "${ACTION}" in
install)
  if [ "$(uname)" != "Darwin" ]; then
    echo "Not macOS. Add this line to the crontab of the user that owns the installation:"
    echo
    echo "  ${MINUTE} ${HOUR} * * *  ${HERE}/scripts/backup.sh '${DESTINATION}' >> ${HERE}/backups/backup.log 2>&1"
    echo
    echo "Reviewed rather than installed: editing a server's crontab unasked is a decision"
    echo "about somebody else's machine."
    exit 0
  fi

  mkdir -p "${HOME}/Library/LaunchAgents" "${DESTINATION}"
  cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${HERE}/scripts/backup.sh</string>
    <string>${DESTINATION}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>${HOUR}</integer>
    <key>Minute</key><integer>${MINUTE}</integer>
  </dict>
  <!-- Both streams kept. A scheduled job's only voice is its log, and \`backup.sh\` says the
       useful things on stderr: the same-disk warning, and the count of document rows whose
       PDF is missing. -->
  <key>StandardOutPath</key><string>${DESTINATION}/backup.log</string>
  <key>StandardErrorPath</key><string>${DESTINATION}/backup.log</string>
  <!-- Deliberately absent: \`RunAtLoad\`. A laptop that has been asleep would otherwise take a
       full backup the moment it wakes, competing with whatever the user came back to do. -->
</dict>
</plist>
PLIST_EOF

  launchctl unload "${PLIST}" 2>/dev/null || true
  launchctl load "${PLIST}"
  echo "Scheduled: daily at $(printf '%02d:%02d' "${HOUR}" "${MINUTE}") -> ${DESTINATION}"
  echo "Log:       ${DESTINATION}/backup.log"

  # The warning `backup.sh` prints per run, repeated at the moment somebody is deciding.
  same_disk() { df -P "$1" 2>/dev/null | awk 'NR == 2 { print $1 }'; }
  if [ "$(same_disk "${DESTINATION}")" = "$(same_disk "${HERE}")" ]; then
    echo
    echo "WARNING: ${DESTINATION} is on the same disk as the installation." >&2
    echo "         Scheduling this only guarantees you will never forget to make a backup" >&2
    echo "         that a disk failure takes with it. Re-run with a path on other hardware." >&2
  fi
  ;;

status)
  if [ "$(uname)" != "Darwin" ]; then
    echo "Not macOS — check the crontab of the user that owns the installation."
    exit 0
  fi
  if launchctl list | grep -q "${LABEL}"; then
    # Read with `PlistBuddy` rather than grepped. The plist is XML and the destination is the
    # second string in an array — a `grep` for it is a parser that works until somebody adds
    # a key above it, which is the kind of thing a status command should not be.
    destination="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:1" "${PLIST}" 2>/dev/null)"
    echo "Scheduled daily at $(printf '%02d:%02d' "${HOUR}" "${MINUTE}")"
    echo "  destination: ${destination}"
    if [ -f "${destination}/backup.log" ]; then
      echo "  last run:"
      tail -n 3 "${destination}/backup.log" | sed 's/^/    /'
    else
      echo "  last run:    none yet"
    fi
  else
    echo "Not scheduled. Run: ./scripts/schedule-backup.sh install [destination]"
  fi
  ;;

remove)
  launchctl unload "${PLIST}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "Removed. Nothing will take a backup now."
  ;;

*)
  echo "usage: schedule-backup.sh {install [destination]|status|remove}" >&2
  exit 1
  ;;
esac
