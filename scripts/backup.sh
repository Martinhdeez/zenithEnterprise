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
# **The destination should not be this machine.** A copy that shares a disk with the thing it
# protects survives a dropped table and a bad migration, and nothing else — not the disk, not
# the laptop, not the room. The default exists so that running this with no arguments does
# something useful, not because `./backups` is a place a backup belongs; the script says so
# out loud at the end when it detects it wrote to the disk it was protecting.
#
# Old backups are pruned to the most recent `ZENITH_BACKUP_KEEP` (default 7). Without that
# this script cannot be scheduled: each run is the size of the whole installation, and an
# unattended job that grows without a bound eventually takes the disk down with it — which is
# a *worse* outcome than the data loss it was guarding against.
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
KEEP="${ZENITH_BACKUP_KEEP:-7}"

mkdir -p "${OUT}"
# Everything in here is readable secrets: user password hashes, the audit trail, every
# document of every tenant, and now the roles file with its SCRAM verifiers. The live
# installation puts all of that behind authentication and RLS; a backup is the one copy where
# none of that applies and a file mode is the only thing left.
chmod 700 "${OUT}"
echo "Backing up to ${OUT}"

# --- 1. the database -----------------------------------------------------------------
#
# Custom format (`-Fc`) rather than plain SQL: it is compressed, and `pg_restore` can read
# its table of contents without replaying it, which is what makes the verification below
# meaningful rather than decorative.
echo "  database ..."
${COMPOSE} exec -T db pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc \
  > "${OUT}/database.dump"

# --- 1b. the roles -------------------------------------------------------------------
#
# `pg_dump` of a *database* cannot contain roles: they are cluster-wide objects. The dump is
# nonetheless full of `GRANT ... TO zenith_app` and `zenith_platform`, so restoring it into a
# fresh cluster fails — and fails as a unit, because the restore runs in one transaction.
#
# This is how a tested restore still loses the data. Ours was exercised against the cluster
# it came from, where the roles already existed, so the one thing that breaks a real recovery
# is precisely the thing that setup could not show. It surfaced when Docker Desktop discarded
# the volume and the backup had to actually work.
#
# `--roles-only`, not `--globals-only`: globals also carry tablespaces, which a containerised
# installation does not have and whose restore would fail on paths that exist on no host here.
echo "  roles ..."
${COMPOSE} exec -T db pg_dumpall -U "${POSTGRES_USER}" --roles-only > "${OUT}/roles.sql"

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
ROLES="$(grep -c '^CREATE ROLE' "${OUT}/roles.sql" || true)"
ROWS="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  'SELECT count(*) FROM documents' | tr -d '[:space:]')"

cat > "${OUT}/manifest.txt" <<EOF
taken            ${STAMP}
database tables  ${TABLES}
database roles   ${ROLES}
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

# Named explicitly rather than "at least one role". `zenith_app` is the role every request
# runs as, so a roles file without it restores a database the application cannot open — which
# looks like a successful recovery right up to the first HTTP request.
if ! grep -q 'CREATE ROLE zenith_app' "${OUT}/roles.sql"; then
  echo "FAILED: zenith_app is missing from roles.sql — this backup cannot be restored" >&2
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

# --- 4. prune the old ones -------------------------------------------------------------
#
# Deliberately *after* the verification above, and unreachable if it failed: pruning on the
# way to a broken backup would delete the last good one to make room for a bad one.
#
# Two conditions to be eligible for deletion, and the second is the important one: the
# timestamp shape, and a `manifest.txt` inside. Only this script writes that file. A backup
# destination is usually a shared disk — a NAS share, a mounted volume, somebody's Dropbox —
# and a prune that trusts a directory name is one wrong `$DESTINATION` away from deleting
# somebody else's directory that happened to be named like a date.
if [ "${KEEP}" -gt 0 ]; then
  CANDIDATES=""
  while IFS= read -r dir; do
    # An `if`, not `[ ... ] && ...`. Under `set -e` an and-list that ends up false *is* a
    # failed command, so the short form would abort the script on the first ineligible
    # directory — the same trap that once killed the diagnostic loop above.
    if [ -f "${dir}/manifest.txt" ]; then
      CANDIDATES="${CANDIDATES}${dir}"$'\n'
    fi
  done < <(find "${DESTINATION}" -mindepth 1 -maxdepth 1 -type d -name '????-??-??T??-??-??Z' | sort)

  TOTAL="$(printf '%s' "${CANDIDATES}" | grep -c . || true)"
  SURPLUS=$(( TOTAL - KEEP ))
  if [ "${SURPLUS}" -gt 0 ]; then
    echo "  pruning ${SURPLUS} of ${TOTAL} backup(s), keeping the newest ${KEEP} ..."
    SEEN=0
    while IFS= read -r dir; do
      [ -n "${dir}" ] || continue
      SEEN=$(( SEEN + 1 ))
      [ "${SEEN}" -le "${SURPLUS}" ] || break
      echo "    removing $(basename "${dir}")"
      rm -rf "${dir}"
    done <<EOF
${CANDIDATES}
EOF
  fi
fi

# --- 5. say where this actually landed --------------------------------------------------
#
# Last, not first: the dump takes minutes and anything printed before it has scrolled off the
# screen by the time it finishes. The final line is the one that gets read.
#
# The database lives in a Docker volume rather than under `$HERE`, but on a single-host
# installation that volume is a file on this same disk, so the project's device is the honest
# proxy for "the disk I am supposed to be protecting".
device() { df -P "$1" 2>/dev/null | awk 'NR == 2 { print $1 }'; }
if [ "$(device "${DESTINATION}")" = "$(device "${HERE}")" ]; then
  echo
  echo "WARNING: ${DESTINATION} is on the same disk as the installation it backs up." >&2
  echo "         This survives a bad migration. It does not survive the disk." >&2
  echo "         Pass a destination on other hardware:  ./scripts/backup.sh /path/on/other/disk" >&2
fi

echo "OK"
