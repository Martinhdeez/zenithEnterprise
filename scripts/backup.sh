#!/usr/bin/env bash
#
# A backup of everything a Zenith installation cannot rebuild.
#
# Two things, and they are not interchangeable. The database holds the tenants, the users,
# the grants, the audit trail and every chunk and vector. The storage directory holds the
# PDFs themselves, content-addressed by sha256. Losing either one alone is unrecoverable:
# the vectors cannot be regenerated without the files, and the files are meaningless without
# the rows that say whose they are and who may read them.
#
# **The database is dumped first, then the files, and the order is not arbitrary.**
# `DocumentService.create` commits the row and *then* writes the file, deliberately, so that
# a rolled-back transaction can never leave an orphan nobody can find. That ordering decides
# this one: dumping the database first means a document uploaded mid-backup is missing from
# the dump but present on disk — an unreferenced file, which is harmless and reclaimable.
# The reverse order produces a row pointing at a file that was never copied.
#
# Neither order gives a perfectly consistent snapshot of a live system. The application
# already tolerates the residual case ("a row whose file is missing … is visible in the
# status column and repairable by re-uploading"), so this is an accepted, documented window
# rather than a hidden one. For a guaranteed-consistent copy, stop the api and worker first.
#
#   ./scripts/backup.sh [destination]      # default: ./backups
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f ${HERE}/docker/docker-compose.yml"
DESTINATION="${1:-${HERE}/backups}"
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
OUT="${DESTINATION}/${STAMP}"

# Read from the same file compose reads, so the backup can never authenticate against a
# different database than the one running.
# shellcheck disable=SC1091
set -a; . "${HERE}/.env"; set +a
POSTGRES_USER="${POSTGRES_USER:-zenith}"
POSTGRES_DB="${POSTGRES_DB:-zenith}"
STORAGE="${HERE}/backend/.data/documents"

mkdir -p "${OUT}"
echo "Backing up to ${OUT}"

# --- 1. the database -----------------------------------------------------------------
#
# Custom format (`-Fc`) rather than plain SQL: it is compressed, and `pg_restore` can read
# its table of contents without replaying it, which is what makes the verification below
# meaningful rather than decorative.
echo "  database ..."
${COMPOSE} exec -T db pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc \
  > "${OUT}/database.dump"

# --- 2. the documents ----------------------------------------------------------------
#
# Tarred rather than copied, because the tree is `<tenant-id>/<sha256>.pdf` and the tenant
# directories are what make a per-tenant restore or purge a single path operation.
echo "  documents ..."
if [ -d "${STORAGE}" ]; then
  tar -czf "${OUT}/documents.tar.gz" -C "${STORAGE}" .
else
  echo "  WARNING: ${STORAGE} does not exist — nothing to archive" >&2
  tar -czf "${OUT}/documents.tar.gz" -T /dev/null
fi

# --- 3. prove it is restorable -------------------------------------------------------
#
# A backup nobody has read is a hope, not a backup. This does not restore anything; it asks
# pg_restore to parse the archive and tar to walk the index, which is enough to catch a
# truncated write, a full disk, or a dump that failed after the file was created.
echo "  verifying ..."
# Streamed on stdin with no file argument. `/dev/stdin` does not survive `docker exec`,
# which is how the first version of this check reported a perfectly good 119 MB dump as
# empty — a verification that cries wolf gets switched off, so it has to be right.
TABLES="$(${COMPOSE} exec -T db pg_restore -l < "${OUT}/database.dump" | grep -c 'TABLE DATA' || true)"
FILES="$(tar -tzf "${OUT}/documents.tar.gz" | grep -c '\.pdf$' || true)"
ROWS="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  'SELECT count(*) FROM documents' | tr -d '[:space:]')"

cat > "${OUT}/manifest.txt" <<EOF
taken            ${STAMP}
database tables  ${TABLES}
document rows    ${ROWS}
pdf files        ${FILES}
database bytes   $(wc -c < "${OUT}/database.dump" | tr -d '[:space:]')
document bytes   $(wc -c < "${OUT}/documents.tar.gz" | tr -d '[:space:]')
EOF

cat "${OUT}/manifest.txt"

if [ "${TABLES}" -eq 0 ]; then
  echo "FAILED: the dump contains no table data" >&2
  exit 1
fi

# Rows without files is the failure this ordering is designed to make rare, and it is worth
# reporting even when the backup itself is fine: it means the *installation* has documents
# whose bytes are already gone, which a restore cannot invent and which nothing else surfaces
# until somebody clicks one in the viewer.
if [ "${ROWS}" -gt "${FILES}" ]; then
  echo "NOTE: ${ROWS} document rows, ${FILES} files — $(( ROWS - FILES )) row(s) have no PDF." >&2
  echo "      Either uploaded during this backup, or already missing from the installation:" >&2
  ${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
    'SELECT tenant_id, count(*) FROM documents GROUP BY 1 ORDER BY 2 DESC' \
    | while IFS='|' read -r tenant count; do
        [ -n "${tenant}" ] || continue
        # `|| true`, and it is load-bearing: `find` on a tenant directory that does not
        # exist — which is exactly the case being reported — exits non-zero, and under
        # `set -e` that killed this loop without printing a word. A diagnostic that dies
        # on the condition it diagnoses is worse than no diagnostic.
        on_disk="$( { find "${STORAGE}/${tenant}" -name '*.pdf' 2>/dev/null || true; } | wc -l | tr -d '[:space:]')"
        [ "${count}" -eq "${on_disk}" ] || echo "        tenant ${tenant}: ${count} rows, ${on_disk} files" >&2
      done
fi

echo "OK"
