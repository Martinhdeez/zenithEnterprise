#!/usr/bin/env bash
#
# Is this installation fit to be demonstrated right now?
#
# Every check here exists because the thing it checks has already gone wrong silently. The
# product is designed to degrade rather than fail — a missing reranker still answers, a
# timed-out embedder still answers from the lexical half — which is right, and which is
# exactly why nobody notices until the answers are visibly worse in front of an audience.
# This is the one command that refuses to be quiet about it.
#
#   ./scripts/demo-check.sh [email] [password]
#
# The credentials are only used to ask the API a real question. Without them the checks that
# need a session are skipped and said to be skipped, rather than passing by omission.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f ${HERE}/docker/docker-compose.yml"
API="${ZENITH_API:-http://localhost:8000}"
WEB="${ZENITH_WEB:-http://localhost:5173}"
EMAIL="${1:-}"
PASSWORD="${2:-}"

FAILURES=0
WARNINGS=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; WARNINGS=$(( WARNINGS + 1 )); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$(( FAILURES + 1 )); }

echo "Demo readiness — ${API}"

# --- the services ---------------------------------------------------------------------
for service in db tei-embed tei-rerank api worker frontend; do
  if [ "$(${COMPOSE} ps -q "${service}" 2>/dev/null | wc -l | tr -d '[:space:]')" -eq 0 ]; then
    bad "${service} is not running"
  else
    ok "${service} up"
  fi
done

# --- what the models actually are ------------------------------------------------------
#
# Not "does it respond". A TEI container serving different weights than the deployment
# intends answers /health identically, and the only symptoms are quality nobody can see and
# latency everybody blames on something else.
for pair in "embed:8081:BAAI/bge-m3" "rerank:8082:"; do
  name="${pair%%:*}"; rest="${pair#*:}"; port="${rest%%:*}"; want="${rest#*:}"
  served="$(curl -fsS "http://localhost:${port}/info" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("model_id",""))' 2>/dev/null || true)"
  if [ -z "${served}" ]; then
    bad "${name} model is not answering on :${port}"
  elif [ -n "${want}" ] && [ "${served}" != "${want}" ]; then
    warn "${name} serving ${served}, expected ${want}"
  else
    ok "${name} serving ${served}"
  fi
done

# --- the proxy ---------------------------------------------------------------------------
#
# A route missing from nginx's list does not 404 — it falls through to the SPA and returns
# `index.html`, so the browser gets HTML where it expects JSON and reports "the request
# failed" with a perfectly healthy API behind it. That has happened twice.
for path in /documents /search /roles /analytics; do
  body="$(curl -s "${WEB}${path}" | head -c 14)"
  case "${body}" in
    *"<!doctype"*|*"<!DOCTYPE"*) bad "${path} falls through to the SPA — missing from the nginx proxy list" ;;
    *)                           ok  "${path} reaches the API through the proxy" ;;
  esac
done

# --- a real question ----------------------------------------------------------------------
if [ -z "${EMAIL}" ] || [ -z "${PASSWORD}" ]; then
  warn "no credentials given — skipping the search check (pass email and password to run it)"
else
  TOKEN="$(curl -fsS -X POST "${API}/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
  if [ -z "${TOKEN}" ]; then
    bad "could not sign in as ${EMAIL}"
  else
    ok "signed in as ${EMAIL}"
    RESULT="$(curl -fsS -H "Authorization: Bearer ${TOKEN}" -G \
      --data-urlencode 'q=plazo maximo de detencion preventiva' "${API}/search" 2>/dev/null || true)"
    read -r HITS TOOK DEGRADED REASON <<EOF
$(printf '%s' "${RESULT}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(len(d.get("hits", [])), d.get("took_ms", 0), d.get("degraded"), (d.get("reason") or "-").replace(" ", "_"))
' 2>/dev/null || echo "0 0 True unreadable")
EOF
    if [ "${HITS}" -eq 0 ]; then
      bad "search returned nothing"
    else
      ok "search returned ${HITS} passages in ${TOOK} ms"
    fi
    # The whole point of this script. `degraded` means the answer came from the fallback,
    # which is a working product and the wrong one to demonstrate.
    if [ "${DEGRADED}" = "True" ]; then
      bad "search is DEGRADED: ${REASON//_/ }"
    else
      ok "search is not degraded"
    fi
    # Slow enough to be noticed from the back of a room. 13 seconds was the number before
    # the cross-encoder was right-sized; anything near it means the wrong model is loaded.
    if [ "${TOOK}" -gt 4000 ]; then
      warn "search took ${TOOK} ms — check which reranker is loaded"
    fi
  fi
fi

# --- the corpus ----------------------------------------------------------------------------
#
# Counted per tenant, never summed across them. A total is the wrong number twice over: it
# is not the corpus anyone will search — every question in the room runs inside one tenant —
# and summing across tenants is exactly how this project once published a passage count
# (21,295) that described no installation that existed. Grouping by tenant also makes a
# second corpus visible as a second corpus rather than as inflation of the first.
STATUSES="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" -tAc \
  "SELECT d.status, count(*), t.name
     FROM documents d JOIN tenants t ON t.id = d.tenant_id
    WHERE t.status <> 'purged'
    GROUP BY d.status, t.name
    ORDER BY count(*) DESC" 2>/dev/null || true)"
if [ -z "${STATUSES}" ]; then
  bad "could not read the corpus"
else
  while IFS='|' read -r status count tenant; do
    [ -n "${status}" ] || continue
    case "${status}" in
      ready)  ok "corpus: ${count} documents ready in ${tenant}" ;;
      failed) bad "corpus: ${count} document(s) failed to ingest in ${tenant}" ;;
      *)      warn "corpus: ${count} document(s) ${status} in ${tenant} — ingestion is still running, and it holds the embedder" ;;
    esac
  done <<EOF
${STATUSES}
EOF
fi

# --- documents that will fail when clicked ------------------------------------------------
#
# A row whose PDF is gone still lists, still searches, and still cites — and then the viewer
# says "That document is no longer available" in front of the audience. The corpus reports
# itself complete everywhere else, which is what makes this worth its own line here rather
# than only in `zenith diagnose`.
# The reading of that report defaults to *not knowing*, never to "fine". The first version
# here parsed the payload as a bare list — it is `{"checks": [...]}` — and its `except` fell
# through to silence, which this script then printed as "every document row has its file" on
# an installation with twenty-six broken ones. A check that reports health when it cannot tell
# is worse than one that cries wolf: nobody switches it off, and nobody looks again.
ORPHANS="$(${COMPOSE} exec -T api zenith diagnose --json 2>/dev/null \
  | python3 -c '
import sys, json
try:
    checks = {c["name"]: c for c in json.load(sys.stdin)["checks"]}
except Exception as error:
    print(f"UNREADABLE could not read the diagnostic report: {error}")
    sys.exit(0)
files = checks.get("document files")
if files is None:
    print("UNREADABLE the diagnostic report has no document-file check")
elif files["status"] != "ok":
    print(files["detail"])
' 2>/dev/null || true)"
case "${ORPHANS}" in
  "")             ok   "every document row has its file" ;;
  UNREADABLE\ *)  warn "${ORPHANS#UNREADABLE }" ;;
  *)              warn "${ORPHANS}" ;;
esac

# --- what the system panel will show ------------------------------------------------------
#
# Everything above asks whether the product works. This asks what a buyer reads, which is a
# different question and the only one with no test behind it. `/system` lists every
# organisation in the installation by name, and this installation grew four of them called
# `M0 baseline <uuid>` during development — three empty. Nothing is broken; the panel is
# doing exactly its job. It is just that opening it in the room shows a page of test
# artefacts, and an evaluator reads that as the state of the product.
#
# A warning and never a failure: the fix is a decision about somebody's data, and this
# script does not get to make it.
LEFTOVERS="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" -tAc \
  "SELECT t.name FROM tenants t
    WHERE t.status = 'active'
      AND NOT EXISTS (SELECT 1 FROM users u WHERE u.tenant_id = t.id)
      AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.tenant_id = t.id)" 2>/dev/null || true)"
if [ -z "${LEFTOVERS}" ]; then
  ok "the system panel lists no empty organisations"
else
  COUNT="$(printf '%s\n' "${LEFTOVERS}" | grep -c .)"
  warn "the system panel will show ${COUNT} active organisation(s) with no users and no documents:"
  # Quoted through a here-doc: an organisation may legitimately be called "Grupo the client",
  # and an unquoted expansion prints that as two organisations.
  while IFS= read -r leftover; do
    [ -n "${leftover}" ] && printf '        %s\n' "${leftover}"
  done <<EOF
${LEFTOVERS}
EOF
  printf '        suspend or purge them from /system before the room, or leave them knowingly\n'
fi

echo
if [ "${FAILURES}" -gt 0 ]; then
  echo "NOT READY — ${FAILURES} failure(s), ${WARNINGS} warning(s)"
  exit 1
fi
[ "${WARNINGS}" -gt 0 ] && echo "Ready, with ${WARNINGS} warning(s)." || echo "Ready."
exit 0
