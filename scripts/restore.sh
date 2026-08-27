#!/usr/bin/env bash
#
# Put a backup back. Destructive, and it says so before doing anything.
#
#   ./scripts/restore.sh backups/2026-08-21T14-00-00Z
#
# The restore is the half of a backup that is usually untested, so this is written to be run
# — on a scratch installation, deliberately, before anyone needs it. A backup whose restore
# has never been executed is a file, not a recovery plan.
#
# **Files first, then the database**, which is the opposite order from the backup and for the
# same reason: whichever is written second is the one that can be mid-write, and a file with
# no row is harmless while a row with no file is a broken document. Backing up puts the
# database at risk of being stale; restoring puts the files at risk instead, so both choose
# the harmless side.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f ${HERE}/docker/docker-compose.yml"
SOURCE="${1:?usage: restore.sh <backup-directory>}"

[ -f "${SOURCE}/database.dump" ] || { echo "no database.dump in ${SOURCE}" >&2; exit 1; }
[ -f "${SOURCE}/documents.tar.gz" ] || { echo "no documents.tar.gz in ${SOURCE}" >&2; exit 1; }

# shellcheck disable=SC1091
set -a; . "${HERE}/.env"; set +a
POSTGRES_USER="${POSTGRES_USER:-zenith}"
POSTGRES_DB="${POSTGRES_DB:-zenith}"
STORAGE="${HERE}/backend/.data/documents"

echo "About to restore ${SOURCE} into the running installation."
[ -f "${SOURCE}/manifest.txt" ] && sed 's/^/  /' "${SOURCE}/manifest.txt"
cat <<EOF

This REPLACES the current database and document storage. Everything created since this
backup was taken will be gone.

EOF
read -r -p "Type the backup's directory name to continue: " CONFIRM
[ "${CONFIRM}" = "$(basename "${SOURCE}")" ] || { echo "Aborted."; exit 1; }

# The api and worker are stopped rather than left running. A worker mid-ingestion would
# write chunks into a database that is being replaced underneath it, and the rows it wrote
# would reference a document the restored dump has never heard of.
echo "Stopping api and worker ..."
${COMPOSE} stop api worker >/dev/null

echo "  documents ..."
mkdir -p "${STORAGE}"
# Cleared rather than merged: a stale file left behind is a document that reappears with the
# right sha256 and the wrong contents for whoever restored, which is worse than a missing
# one because nothing flags it.
find "${STORAGE}" -mindepth 1 -delete
tar -xzf "${SOURCE}/documents.tar.gz" -C "${STORAGE}"

echo "  roles ..."
# Before the dump, and this ordering is a hard requirement rather than a preference: the dump
# is full of `GRANT ... TO zenith_app`, and a grant to a role that does not exist is an error.
# The restore runs in one transaction, so that single error discards the entire recovery.
#
# Roles are cluster-wide, so they are absent from a fresh cluster — a reinstalled Docker
# Desktop, a new host, the disaster this whole script exists for. The old cluster was the only
# place they were guaranteed to already be there, which is exactly why restoring into it
# proved nothing.
if [ -f "${SOURCE}/roles.sql" ]; then
  # Deliberately not `ON_ERROR_STOP`: re-running a restore into a cluster that already has
  # these roles raises "role already exists", which is the expected case and not a problem.
  # The errors are not swallowed either — they print. What is asserted is the postcondition
  # below, which is the thing that actually has to be true.
  ${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d postgres < "${SOURCE}/roles.sql" >/dev/null || true

  MISSING_ROLES=""
  for role in $(grep -oE '^CREATE ROLE [a-zA-Z0-9_]+' "${SOURCE}/roles.sql" | awk '{print $3}'); do
    present="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d postgres -tAc \
      "SELECT 1 FROM pg_roles WHERE rolname = '${role}'" | tr -d '[:space:]')"
    [ "${present}" = "1" ] || MISSING_ROLES="${MISSING_ROLES} ${role}"
  done
  if [ -n "${MISSING_ROLES}" ]; then
    echo "FAILED: roles could not be created:${MISSING_ROLES}" >&2
    echo "        The database restore would abort on the first GRANT. Stopping here," >&2
    echo "        with the current database untouched." >&2
    ${COMPOSE} start api worker >/dev/null
    exit 1
  fi
  echo "  roles present"
else
  # A backup taken before roles were captured. Say so plainly: it is restorable into the
  # cluster it came from and nowhere else, which is the opposite of what a backup is for.
  echo "  WARNING: no roles.sql in this backup." >&2
  echo "           It can only be restored into a cluster that already has zenith_app." >&2
fi

echo "  database ..."
# `--clean --if-exists` inside a single transaction: either the whole schema is replaced or
# none of it is, so a failure halfway does not leave a half-restored database that looks
# like a working one.
${COMPOSE} exec -T db pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  --clean --if-exists --single-transaction < "${SOURCE}/database.dump"

echo "Starting api and worker ..."
${COMPOSE} start api worker >/dev/null

echo "  verifying ..."
ROWS="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  'SELECT count(*) FROM documents' | tr -d '[:space:]')"
FILES="$(find "${STORAGE}" -name '*.pdf' | wc -l | tr -d '[:space:]')"
echo "  ${ROWS} document rows, ${FILES} pdf files"

# Not a count comparison against the manifest — the manifest may legitimately disagree by a
# document uploaded during the backup. This is the check that matters: every row must have
# its file, because that is the direction that breaks the product.
MISSING=0
while IFS='|' read -r tenant sha; do
  [ -n "${sha}" ] || continue
  [ -f "${STORAGE}/${tenant}/${sha}.pdf" ] || MISSING=$(( MISSING + 1 ))
done < <(${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  'SELECT tenant_id, sha256 FROM documents')

if [ "${MISSING}" -gt 0 ]; then
  echo "WARNING: ${MISSING} document row(s) have no PDF on disk." >&2
  echo "         They will show as unavailable in the viewer and can be re-uploaded." >&2
else
  echo "OK — every document row has its file."
fi
