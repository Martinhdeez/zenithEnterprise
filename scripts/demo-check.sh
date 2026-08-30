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
# Asking a question costs one real model call. `ZENITH_DEMO_SKIP_ANSWER=1` leaves it out —
# see the reasoning above that check. It runs by default, because the point of it is to catch
# what everything else here missed.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f ${HERE}/docker/docker-compose.yml"
API="${ZENITH_API:-http://localhost:8000}"
WEB="${ZENITH_WEB:-http://localhost:5173}"
EMAIL="${1:-}"
PASSWORD="${2:-}"
SKIP_ANSWER="${ZENITH_DEMO_SKIP_ANSWER:-}"

# One question, asked twice: once of `/search` and once of `/query`. Two strings here would
# drift, and the day they did the answer check would stop being able to lean on the search
# check above it — "the model was given nothing" and "the corpus holds nothing" are different
# findings, and only one question asked of both endpoints can tell them apart.
QUESTION="plazo maximo de detencion preventiva"

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

# --- has the reranker been dying and coming back? -----------------------------------------
#
# The one question this script can answer and `zenith diagnose` cannot. The diagnostic runs
# inside the API container, where Docker's restart count is out of reach, so it has to infer a
# restart from TEI's own counters — which reset with the process and cannot say how many times
# or when. This script runs on the host with the Docker CLI, so it can simply ask.
#
# It is the question nobody was asking on 28 August. `tei-rerank` was killed for memory
# (exit 137), came back, and search kept answering from the fused order about fifteen points
# of recall worse in between. Every check that only asked "is the container listed" said yes:
# a container that is restarting in a loop has an id like any other, and the loop above would
# have called it up.
#
# A failure, not a warning, when it is recent. A service that is up now but died twice in the
# last ten minutes is not fit to demonstrate — the recall the audience sees depends on which
# side of a kill their question lands on.
# `-a`, unlike the loop above: a container that is stopped or looping still has an id and a
# restart count, and those are exactly the two states worth asking about here.
RERANK_ID="$(${COMPOSE} ps -aq tei-rerank 2>/dev/null | head -1)"
if [ -n "${RERANK_ID}" ]; then
  read -r STATE RESTARTS STARTED <<EOF
$(docker inspect --format '{{.State.Status}} {{.RestartCount}} {{.State.StartedAt}}' \
    "${RERANK_ID}" 2>/dev/null || echo "unknown 0 -")
EOF
  # Seconds since the *current* process started. Docker's timestamp carries nanoseconds,
  # which `fromisoformat` will not parse, so the fraction is dropped rather than rounded —
  # this is a "how long ago, roughly" and a second either way changes nothing.
  AGE="$(python3 -c '
import datetime, sys
stamp = sys.argv[1].split(".")[0].rstrip("Z")
started = datetime.datetime.fromisoformat(stamp).replace(tzinfo=datetime.timezone.utc)
print(int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()))
' "${STARTED}" 2>/dev/null || echo -1)"
  if [ "${STATE}" = "restarting" ]; then
    # The state the loop above cannot see. A container caught between kills is listed like
    # any other, so that loop calls it up — which is precisely how 28 August went unnoticed.
    bad "tei-rerank is restarting — it is in a loop right now, and the check above still calls it up"
  elif [ "${STATE}" != "running" ]; then
    : # Already reported as not running, by name, in the loop above.
  elif [ "${RESTARTS}" -eq 0 ] 2>/dev/null; then
    ok "tei-rerank has not restarted since it was created"
  elif [ "${AGE}" -ge 0 ] && [ "${AGE}" -lt 1800 ]; then
    bad "tei-rerank has restarted ${RESTARTS} time(s), the last $(( AGE / 60 ))m ago — 'docker logs' it and look for exit 137"
  else
    warn "tei-rerank has restarted ${RESTARTS} time(s), but has been up for $(( AGE / 60 ))m"
  fi
fi

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
  warn "no credentials given — skipping the search and answer checks (pass email and password to run them)"
else
  TOKEN="$(curl -fsS -X POST "${API}/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
  if [ -z "${TOKEN}" ]; then
    bad "could not sign in as ${EMAIL}"
  else
    ok "signed in as ${EMAIL}"
    RESULT="$(curl -fsS -H "Authorization: Bearer ${TOKEN}" -G \
      --data-urlencode "q=${QUESTION}" "${API}/search" 2>/dev/null || true)"
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

    # --- and a real answer ------------------------------------------------------------------
    #
    # Everything above this line is retrieval, and retrieval is not what a demonstration is. A
    # demonstration is somebody typing a question and reading a written answer with citations
    # under it, and until this block existed that was the one path this script never walked.
    #
    # On 30 August every line above it printed green — every container up, both model services
    # serving the right weights, every prefix reaching the API, signed in, eight passages in
    # 800 ms, not degraded, the corpus intact, the reranker healthy, the lock budget fine — on
    # an installation whose `POST /query` was answering 503 because the tenant's model key was
    # rate-limited. It printed `Ready.` and it would have sent somebody into a room.
    #
    # **It writes, and that is not worked around.** `POST /query` records a row in `queries`
    # and one per citation in `query_citations`. That is the endpoint's contract and there is
    # no read-only variant of the answer path to substitute, so the row stays: deleting it
    # would be a second write to the customer's database to hide the first, and a readiness
    # check has no business editing what it is inspecting. Two consequences worth knowing
    # before the room — the question shows up in the history panel and in the analytics count,
    # and it is deliberately the *same* question the search check just asked, so an operator
    # who opens history sees one recognisable line rather than a mystery.
    #
    # **And it costs a real model call** — about nine seconds of model time by F9's
    # measurement, and on a metered provider a fraction of a cent and one unit of rate-limit
    # budget. That last one is not hypothetical: the failure this block exists to catch *was*
    # a rate limit, and a check run in a loop can manufacture the very 429 it reports. Hence
    # `ZENITH_DEMO_SKIP_ANSWER=1`, for the operator iterating on something else. Hence also
    # that the default is to run — a check nobody runs catches nothing, and this is the one
    # that everything else missed — and that skipping announces itself as a warning rather
    # than passing by omission, exactly as the missing-credentials case does.
    if [ -n "${SKIP_ANSWER}" ]; then
      warn "ZENITH_DEMO_SKIP_ANSWER is set — nobody has asked this installation a question"
    else
      # `--max-time` rather than curl's default of none: a model that never replies is a hang,
      # and a check that hangs is one an operator learns to run without. Sixty seconds is
      # generous against F9's nine, so anything that reaches it is broken and not merely slow.
      #
      # `-s` and not `-fsS`, unlike every other call in this file. `-f` throws the body away
      # on a 4xx or 5xx, and on this endpoint the body *is* the finding — the difference
      # between the four endings below is written in it.
      ANSWER="$(curl -s --max-time 60 -w '\n%{http_code}' -X POST "${API}/query" \
        -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
        --data "$(python3 -c 'import json,sys;print(json.dumps({"question": sys.argv[1]}))' \
          "${QUESTION}")" 2>/dev/null || true)"
      CODE="${ANSWER##*$'\n'}"
      BODY="${ANSWER%$'\n'*}"

      # Six endings, named separately, because collapsing them is the bug this part of the
      # product has been bitten by three times — the `Outcome` enum in
      # `features/ingestion/classification.py` is the same argument for the same reason, and
      # its docstring is worth reading before touching this.
      #
      #   CITED         an answer with at least one citation. What a demonstration shows.
      #   ABSTAINED     it read the passages and would not answer. Correct behaviour, and a
      #                 pass — the safeguard doing its job is not a fault to report.
      #   UNCITED       an answer citing nothing. Invariant 5 says this cannot happen, so
      #                 seeing it says this *installation* is not running the binder that
      #                 enforces it. No test built from the code can see that.
      #   NOTHING_READ  it abstained having retrieved nothing, so no model was ever asked.
      #                 Distinct from the search check above it, which cannot stand in for
      #                 this: `/query` rewrites the question through `routing.resolve` before
      #                 retrieving, so the two can disagree and only one of them is the path
      #                 the room will use.
      #   UNAVAILABLE   503. Two facts wearing one status code; split below.
      #   everything else — throttled, forbidden, unreadable — said as itself.
      #
      # No ending is allowed to be silent, and none of them defaults to "fine": a check that
      # reports health when it cannot tell is worse than one that cries wolf, because nobody
      # switches it off and nobody looks again.
      VERDICT="$(printf '%s' "${BODY}" | python3 -c '
import sys, json
status = sys.argv[1]
raw = sys.stdin.read().strip()
if status in ("000", ""):
    print("NO_REPLY /query did not reply within 60s — the model call is hanging, not slow")
    sys.exit(0)
try:
    body = json.loads(raw)
except Exception:
    print("UNREADABLE /query answered " + status + " with something that is not JSON: " + raw[:120])
    sys.exit(0)
detail = body.get("detail") or body.get("message") or "with no detail given"
if status != "200":
    print({"503": "UNAVAILABLE ", "429": "THROTTLED ", "403": "FORBIDDEN "}.get(
        status, "REFUSED /query answered " + status + ": ") + detail)
    sys.exit(0)
citations = body.get("citations") or []
consulted = body.get("consulted") or []
model = body.get("model") or "an unnamed model"
if body.get("abstained"):
    if consulted:
        print("ABSTAINED it read " + str(len(consulted)) + " passage(s) and would not answer from them")
    else:
        print("NOTHING_READ /query retrieved nothing for this question, so no model was asked")
elif citations:
    print("CITED " + model + " answered in " + str(body.get("took_generation_ms") or 0)
          + " ms, citing " + str(len(citations)) + " of " + str(len(consulted)) + " passage(s)")
else:
    print("UNCITED /query returned an answer that cites nothing, which invariant 5 forbids")
' "${CODE:-000}" 2>/dev/null || printf 'UNREADABLE the answer could not be read at all\n')"

      case "${VERDICT}" in
        CITED\ *)        ok   "${VERDICT#* }" ;;
        # A pass, and it has to read like one. An abstention is the product working: the
        # model was shown the passages and declined, which is the behaviour invariant 5
        # exists to produce. It is also why the question cannot be chosen to guarantee a
        # citation — nothing this script can do makes a model answer — so the check is built
        # so that it does not need to. What it asserts is that the whole path ran: retrieval,
        # prompt, model, citation binding. Which of the two legitimate endings it reached is
        # reported, not graded.
        ABSTAINED\ *)    ok   "the model abstained rather than answer — ${VERDICT#* }" ;;
        UNCITED\ *)      bad  "${VERDICT#* }" ;;
        NOTHING_READ\ *) bad  "${VERDICT#* }" ;;
        NO_REPLY\ *)     bad  "${VERDICT#* }" ;;
        # This installation's own limiter, not the model's, and the two are easy to confuse
        # because both are 429: the model's arrives as the 503 below, quoting a number from
        # somebody else's API. A warning, because it says the endpoint works and this account
        # has asked too often — which running this script repeatedly is one way to achieve.
        THROTTLED\ *)    warn "the API throttled the question — its own rate limit, not the model's: ${VERDICT#* }" ;;
        FORBIDDEN\ *)    warn "${EMAIL} may not ask questions — run this as the account that will be demonstrated: ${VERDICT#* }" ;;
        UNAVAILABLE\ *)
          # 503 is two facts wearing one status code. `GenerationUnavailableError` is raised
          # both by an installation nobody configured a model for and by a model that is
          # configured and broken, and those have nothing in common except the number.
          #
          # Split by asking `/llm-config`, which answers it structurally, rather than by
          # matching words in the detail: the detail is prose written for the person who asked
          # the question, and a check that greps it breaks the day somebody improves the
          # sentence. The test is the same one `connector/providers.build` applies — an
          # endpoint and a model name, both non-empty — so the two cannot drift apart.
          #
          # **When it cannot be established, it fails.** `/llm-config` needs
          # `llm_config.manage` and the demonstrating account may not hold it. A false alarm
          # costs somebody two minutes on the admin screen; being wrong the other way is what
          # happened on 30 August.
          CONFIGURED="$(curl -fsS --max-time 10 -H "Authorization: Bearer ${TOKEN}" \
            "${API}/llm-config" 2>/dev/null | python3 -c '
import sys, json
config = json.load(sys.stdin)
print("yes" if config.get("endpoint_url") and config.get("model_name") else "no")
' 2>/dev/null || true)"
          case "${CONFIGURED}" in
            # An ordinary, supported installation — search and ingestion and isolation all
            # work without generation, and a demonstration of those is a real demonstration.
            # A warning and not a failure for the reason the leftover-organisations check
            # below is one: what this room is about is somebody else's decision, and a script
            # that declares NOT READY is telling them not to walk in. It is also the only
            # ending here that cannot ambush anybody — it says the same sentence to the first
            # question and the hundredth, and its remedy is an admin form.
            no)  warn "no language model is configured — this installation can search but cannot answer, so the chat is not demonstrable. Configure one under Admin, or plan to show search: ${VERDICT#* }" ;;
            yes) bad  "a language model IS configured and it is not answering: ${VERDICT#* }" ;;
            *)   bad  "the answer path is down and this check could not establish whether a model is configured — /llm-config needs llm_config.manage. Read it as broken until somebody looks: ${VERDICT#* }" ;;
          esac
          ;;
        *)               warn "${VERDICT#UNREADABLE }" ;;
      esac
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

# --- what `zenith diagnose` found -----------------------------------------------------------
#
# Asked once and read whole. The diagnostic opens the database, walks the storage root and
# calls both model services; running it once per question would pay for all of that again to
# learn nothing new.
#
# **Read whole is the correction.** Until 30 August this block fetched all eighteen answers
# and looked at three — `document files`, `reranker health`, `lock budget` — and dropped the
# other fifteen without a word. One of the fifteen was `migrations`, which is the trap
# CLAUDE.md names in its own sentence: *a green suite does not mean the database is migrated,
# and a missing migration is a 500 in production with CI green.* The one command whose whole
# purpose is to ask whether this **installation** is fit to show was holding that answer in a
# variable and not looking at it. Another was `hardware profile`, which on this machine says
# OCR is off — so a scanned PDF handed over in the room is refused rather than ingested, and
# nobody running this script was told before they were asked for it.
#
# So the reading is no longer a list of names. It walks the report, and a check the script has
# never heard of is reported like any other. That direction is deliberate: a name-by-name
# reader says nothing about whatever nobody remembered to add to it, and that silence is
# exactly how fifteen answers went missing. The list that remains is an opt-**out**, it holds
# only checks a line above already answers, and it can silence nothing but an `ok`.
#
# **The severity is the diagnostic's own, and that is a judgement rather than a shortcut.**
# `demo-check` asks a narrower question than `diagnose` does — "fit to show", not "healthy" —
# so the two could have disagreed, and check by check they do not. Every `fail` the diagnostic
# can produce is a reason not to walk in: RLS inactive, a database behind head, an undeclared
# bypass function, a missing extension, no active vector space, a storage root that cannot be
# written, a queue that will never ingest what somebody uploads in the room, a document row
# whose file is gone. And every `warn` it can produce is a thing to know on the way in rather
# than a reason to cancel: a modulus that could be better, a document stranded out of the
# queue, a profile with OCR turned off. That last is the sharp case, and it stays a warning
# for the same reason the leftover-organisations check below is one — the installation works,
# it works less well, and what this room is about is somebody else's decision to make.
#
# Three things the named checks used to carry, kept because the report does not say them:
#
#   `reranker health` is the other half of the Docker restart count read near the top of this
#   file. That block asks how many times the container has died and when; this asks whether
#   the component is reachable and serving *right now*, and whether the circuit breaker has
#   been skipping it. Neither answers the other.
#
#   `lock budget` is the one failure in the whole report that is not a degradation. A
#   partitioned installation whose `max_locks_per_transaction` is too small does not answer
#   worse: Postgres raises `OutOfMemory` while *planning* and an ordinary search is a 500. It
#   arrives at whatever concurrency the room produces, so the first person to ask a question
#   sees it work and the fourth does not, and the setting needs a restart — which is not
#   something to do between slides.
#
#   `document files` was a warning here and is now the diagnostic's own failure. A row whose
#   PDF is gone lists, searches and cites perfectly, and then the viewer says "That document
#   is no longer available" in front of the audience. That is not a degradation to mention on
#   the way in; it is the demonstration breaking while somebody watches. The warning was never
#   argued for — the sentence above it argued for a failure and the code said `warn`.
#
# The reading of that report defaults to *not knowing*, never to "fine". The first version
# here parsed the payload as a bare list — it is `{"checks": [...]}` — and its `except` fell
# through to silence, which this script then printed as "every document row has its file" on
# an installation with twenty-six broken ones. A check that reports health when it cannot tell
# is worse than one that cries wolf: nobody switches it off, and nobody looks again. So an
# unreadable report is now a failure rather than three warnings, and an **empty** one is too:
# both mean this script learned nothing at all from a command it has already paid for.
REPORT="$(${COMPOSE} exec -T api zenith diagnose --json 2>/dev/null || true)"

# One line per check, as `STATUS<tab>name<tab>detail`. Never empty and never silent.
#
# `QUIET` is the whole of the discrimination, and every entry is there because a line above
# this one already answers it better — not because the detail is long:
#
#   `configuration` and `database (application role)` have no failing branch to report. The
#   first prints redacted connection strings, token lifetimes and pool sizes; the second
#   prints the role name and the server version. If the database were unreachable, `content`,
#   `migrations` and the corpus block would all say so first and louder.
#
#   `content` counts rows across every tenant. The corpus block above counts them *per tenant*
#   on purpose, and its comment says why: a total is the wrong number twice over, and this
#   project once published one — 21,295 passages — that described no installation that
#   existed. Printing it here would put that number back.
#
#   `embedding service` and `reranking service` ask what the `/info` loop at the top of this
#   file already asks, and that loop asks it harder: it holds the served model against the
#   name the deployment intends. What these two add is the vantage point — they call from
#   inside the API container, so they disagree with that loop precisely when the container
#   network is broken and the host's port mapping is not. A disagreement is never an `ok`, so
#   it is never quiet.
#
# `REQUIRED` is not a list of what to report; the report decides that. It is the list of names
# this file's own prose leans on, so that a check quietly *disappearing* from the diagnostic
# is caught. That is the same failure as one that was never read, arriving from the other
# direction, and `migrations` is the one it must never happen to.
DIAGNOSIS="$(printf '%s' "${REPORT}" | python3 -c '
import sys, json

QUIET = {
    "configuration",
    "database (application role)",
    "content",
    "embedding service",
    "reranking service",
}
REQUIRED = ("migrations", "document files", "reranker health", "lock budget")

# Every line carries all three fields, always. A tab is IFS *whitespace* to the shell builtin
# that reads these back, so two of them in a row collapse into one delimiter and a line that
# skipped the middle field arrives with its detail in the name — which is how the count line
# below went missing the first time it was written.
SUBJECT = "zenith diagnose"

raw = sys.stdin.read().strip()
if not raw:
    print(f"UNREADABLE\t{SUBJECT}\tit produced no output at all")
    sys.exit(0)
try:
    checks = json.loads(raw)["checks"]
except Exception as error:
    print(f"UNREADABLE\t{SUBJECT}\tits report could not be read: {error}")
    sys.exit(0)
if not checks:
    print(f"UNREADABLE\t{SUBJECT}\tits report is empty — it ran and checked nothing")
    sys.exit(0)

lines = []
quiet = 0
for check in checks:
    name = str(check.get("name") or "an unnamed check")
    status = str(check.get("status") or "").lower()
    detail = str(check.get("detail") or "") or "with no detail given"
    if status == "ok" and name in QUIET:
        quiet += 1
    elif status in ("ok", "warn", "fail"):
        lines.append(f"{status.upper()}\t{name}\t{detail}")
    else:
        # A status this script has never seen is not a pass. Reported as a warning, and with
        # the word itself, so whoever added the tier can see where it arrives.
        lines.append(f"WARN\t{name}\treported an unrecognised status {status!r}: {detail}")

present = {str(c.get("name")) for c in checks}
for name in REQUIRED:
    if name not in present:
        lines.append(
            f"FAIL\t{name}\tthe diagnostic report no longer has a {name} check, "
            f"and this script was written expecting one"
        )

counted = f"{len(checks)} check(s) read from the report"
print(f"NOTE\t{SUBJECT}\t{counted}, {quiet} of them quiet because a line above answers them"
      if quiet else f"NOTE\t{SUBJECT}\t{counted}, all of them reported")
if lines:
    print("\n".join(lines))
' 2>/dev/null || printf 'UNREADABLE\tzenith diagnose\tits report could not be read at all\n')"

while IFS="$(printf '\t')" read -r STATUS NAME DETAIL; do
  case "${STATUS}" in
    OK)         ok   "${NAME}: ${DETAIL}" ;;
    WARN)       warn "${NAME}: ${DETAIL}" ;;
    FAIL)       bad  "${NAME}: ${DETAIL}" ;;
    UNREADABLE) bad  "${NAME}: ${DETAIL}" ;;
    # Indented to the detail column, like the leftover organisations below: it is not a check
    # and must not read as one. It exists so the count is visible — the day `diagnose` grows a
    # nineteenth check this line says nineteen, and the day one stops being read it is how
    # somebody notices.
    NOTE)       printf '        %s\n' "${DETAIL}" ;;
  esac
done <<EOF
${DIAGNOSIS}
EOF

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
  # Quoted through a here-doc: an organisation may legitimately be called "Example Group",
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
