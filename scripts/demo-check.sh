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

# Set by the sign-in below and read again by the corpus block, which runs after it. Declared
# here so that "nobody signed in" is an empty string rather than an unbound variable under
# `set -u`, and so that the one place it is set is greppable.
TOKEN=""

FAILURES=0
WARNINGS=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; WARNINGS=$(( WARNINGS + 1 )); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$(( FAILURES + 1 )); }

echo "Demo readiness — ${API}"

# --- the services ---------------------------------------------------------------------
#
# **Not "is the container listed".** This loop used to ask `compose ps -q` for an id and print
# `up` when it got one, and three states answer that question with a yes while serving
# nothing: a container restarting in a loop has an id like any other, a paused one is listed
# like a healthy one, and a container that has just come back from a kill looks exactly like
# one that has been up for a week.
#
# It is the question nobody was asking on 28 August. `tei-rerank` was killed for memory
# (exit 137), came back, and search kept answering from the fused order about fifteen points
# of recall worse in between, marked `degraded` in a field nobody was reading. Every check
# that only asked "is the container listed" said yes.
#
# **And until today only `tei-rerank` was asked properly.** The other five were counted by
# id, which makes `worker` the indefensible one: a worker in a restart loop prints `worker up`
# — the exact 28-August failure mode, in the service that ingests — and `job queue: 0 job(s)
# waiting` below reads identically whether the queue is idle or nothing is draining it. No
# service here deserves less than the reranker got. A `db` that has restarted twice in ten
# minutes has dropped every connection twice; an `api` in a loop answers one request in
# three; a `frontend` restarting is the proxy disappearing between two slides.
#
# This is also the one question `zenith diagnose` cannot answer. The diagnostic runs inside
# the API container, where Docker's restart count is out of reach, so it has to infer a
# restart from TEI's own counters — which reset with the process and cannot say how many
# times or when. This script runs on the host with the Docker CLI, so it can simply ask.
#
# A failure, not a warning, when a restart is recent. A service that is up now but died twice
# in the last ten minutes is not fit to demonstrate — what the audience sees depends on which
# side of a kill their question lands on.
#
# `-aq` rather than `-q`: a container that is stopped or looping still has an id and a restart
# count, and those are exactly the two states worth asking about.
for service in db tei-embed tei-rerank api worker frontend; do
  CONTAINER="$(${COMPOSE} ps -aq "${service}" 2>/dev/null | head -1)"
  if [ -z "${CONTAINER}" ]; then
    bad "${service} has no container at all — it was never created, or 'compose down' removed it"
    continue
  fi
  # `unreadable` rather than `unknown`, and the word is load-bearing: it must be a token
  # Docker can never itself return, because the arms below dispatch on the state name and the
  # one thing that must not happen is a real state and a failure to read one arriving as the
  # same string. `unknown` was not safe on that count and was not treated as a finding either;
  # see below.
  read -r STATE RESTARTS STARTED <<EOF
$(docker inspect --format '{{.State.Status}} {{.RestartCount}} {{.State.StartedAt}}' \
    "${CONTAINER}" 2>/dev/null || echo "unreadable 0 -")
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
  if [ "${STATE}" = "unreadable" ]; then
    # `docker inspect` failed, and until 30 August that was the quietest outcome in this
    # block. The fallback said `unknown`, `unknown` is not `running`, so it landed on a silent
    # arm on top of a green line claiming the container was up. The one outcome meaning "this
    # check learned nothing" was the one outcome that said nothing.
    #
    # A failure and not a warning, for the reason the 503 split further down gives in the same
    # words: when it cannot be established, it fails.
    bad "${service} is listed but 'docker inspect' could not read it — its state and restart count are unknown, and all this check knows is that a container has an id"
  elif [ "${STATE}" = "restarting" ]; then
    bad "${service} is restarting — it is in a loop right now, and an id is all a container in a loop needs to look healthy"
  elif [ "${STATE}" = "exited" ] || [ "${STATE}" = "dead" ] || [ "${STATE}" = "created" ] || [ "${STATE}" = "removing" ]; then
    bad "${service} is ${STATE} — it is not running"
  elif [ "${STATE}" != "running" ]; then
    # Anything else. `paused` is the one that exists today and it is not hypothetical enough
    # to ignore: a paused container is listed by `compose ps` like a healthy one and answers
    # nothing. A state named neither here nor above is not a pass either — the same rule the
    # diagnose reader applies to a status it has never seen.
    bad "${service} is ${STATE} — it is listed, and it is not serving"
  elif [ "${RESTARTS}" -eq 0 ] 2>/dev/null; then
    ok "${service} up, no restarts since it was created"
  elif [ "${AGE}" -lt 0 ]; then
    # It has restarted and the timestamp could not be parsed, so *when* has no answer. This
    # used to fall through to the `warn` below and print "restarted 3 time(s), but has been up
    # for 0m", because bash truncates `-1 / 60` toward zero — and "up for 0m" is the
    # recent-restart case, the one this block calls a failure. The worst reading of the
    # evidence was printed in the words of the mildest one.
    bad "${service} has restarted ${RESTARTS} time(s) and this check could not read when it last started (${STARTED}) — 'docker compose logs ${service}'; a container killed for memory exits 137"
  elif [ "${AGE}" -lt 1800 ]; then
    bad "${service} has restarted ${RESTARTS} time(s), the last $(( AGE / 60 ))m ago — 'docker compose logs ${service}'; a container killed for memory exits 137"
  else
    warn "${service} has restarted ${RESTARTS} time(s), but has been up for $(( AGE / 60 ))m"
  fi
done

# --- and is the worker consuming anything? ------------------------------------------------
#
# The loop above knows the container is running and has not been dying. Neither fact says the
# process inside it is doing its job, and `job queue: 0 job(s) waiting` further down cannot
# help: an empty queue reads identically whether a healthy worker has drained it or a dead one
# never touched it, because nothing was asked of it either way. What that leaves unguarded is
# a document uploaded in the room that stays `pending` for ever with every line here green.
#
# procrastinate's own heartbeat answers it, and it is a fact about the process rather than
# about the queue. A worker registers a row in `procrastinate_workers` and updates
# `last_heartbeat` every ten seconds; a worker that stops leaves the row behind with a
# timestamp that stops moving, because the pruning of stale workers is done by *another*
# running worker. So a stale heartbeat is the shape of a worker that died, and no heartbeat at
# all is the shape of one that never started — and the two want different sentences.
#
# Sixty seconds is six missed beats: wide enough that a worker busy inside one long embedding
# call is not called dead, narrow enough to catch one killed on the way into the room.
#
# The owner connection, for the reason `_job_queue` in `core/diagnostics.py` gives: the queue
# tables are procrastinate's own, they carry no RLS, and `zenith_app` holds no privilege on
# them by design — asking with the application role reports `permission denied` on a perfectly
# healthy installation.
HEARTBEAT="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" -tAc \
  "SELECT coalesce(max(extract(epoch FROM now() - last_heartbeat))::bigint, -1)
     FROM procrastinate_workers" 2>/dev/null)"
ASKED=$?
if [ "${ASKED}" -ne 0 ]; then
  # A warning rather than a second failure, for the same reason the corpus comparison below
  # is: every cause of this — a `db` that will not answer, queue tables that were never
  # installed — is already a failure somewhere else in this report, and one cause counted
  # twice reads at the bottom like two problems.
  warn "could not ask whether a worker is consuming the queue — psql exited ${ASKED}; the 'job queue' check below says whether procrastinate's tables are installed at all"
elif [ "${HEARTBEAT}" -lt 0 ] 2>/dev/null; then
  bad "no worker has ever registered a heartbeat — nothing is draining the queue, so a document uploaded in the room stays 'pending' and is never searchable"
elif [ "${HEARTBEAT}" -gt 60 ]; then
  bad "the last worker heartbeat was ${HEARTBEAT}s ago — the container is up and the process inside it has stopped consuming the queue"
else
  ok "worker heartbeat ${HEARTBEAT}s old — it is consuming the queue"
fi

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
#
# **And this check passed against a port with nothing behind it**, which is the same class of
# bug as the one it exists to catch, sitting inside the guard against it.
# `ZENITH_WEB=http://localhost:9999 ./scripts/demo-check.sh` printed four greens. It asked
# whether the first fourteen bytes of the body were a doctype, and an empty body is not a
# doctype — so a refused connection passed, a stopped `frontend` container passed, and so did
# nginx's own 502 page, which begins `<html>` with no doctype at all. The one thing it could
# see was the exact byte string it was looking for.
#
# There are three situations, and the doctype test could not tell any of them apart:
#
#   nothing answered   curl never got a response. The `frontend` container is not serving, or
#                      `ZENITH_WEB` names the wrong port. Nothing at all is known about the
#                      proxy list, because nothing was ever asked — and that is the finding.
#   HTML answered      either the prefix is missing from nginx's list and the SPA took the
#                      request (the trap: 200, `index.html`), or nginx matched the prefix and
#                      could not reach the API behind it, and answered its own error page
#                      (502). Different remedies — edit two config files, or start `api` —
#                      so they are told apart by the status rather than merged.
#   JSON answered      the request crossed the proxy and something that speaks this API's
#                      language replied.
#
# **The assertion is that the body parses as JSON, and it is the client's own test.**
# `request()` in `api/client.ts` calls `response.json()`, and the trap is precisely that call
# throwing on `<!doctype`. Reproducing it is the only test that cannot pass against nothing:
# an empty body is not JSON either.
#
# Deliberately *not* the status. Every one of these answers 401, 404 or 405 here, because
# this runs without a token and asks for the bare prefix, and all three are a pass: the
# question is where the request arrived, not what it was allowed to do once there. Pinning
# the status would make the check fail the day one of these routes stops needing a token,
# which is not what it is watching for. Deliberately not the content type on its own either —
# it is a header, set by whatever answered, and what the browser chokes on is the bytes. The
# statuses are still printed, because an operator reading `401` learns something an operator
# reading `OK` does not.
#
# **The list of prefixes is no longer written here.** Four were, out of the twelve the API
# serves, and the two this product has actually shipped broken — `groups` and `system` — were
# not among the four. Writing twelve down would make this the *third* hand-kept copy of the
# route table, after nginx's and vite's, and CONTRIBUTING.md's rule about registries applies
# to it exactly: a file that every new router has to be added to is wrong the one time
# somebody forgets, and the fix is to make the file discover its entries rather than list
# them. So the list is asked of the running API, which is also the only source that suits
# this script. `tests/integration/test_proxy_prefixes.py` already holds the two config
# *files* to the route table the code declares; that is the declared question. This one is
# whether the installation that is running right now answers on every prefix it says it
# serves — and a `frontend` image built before the last router landed fails here and passes
# there.
#
# `health` and the schema documents are excluded for the reason that test excludes them:
# nginx serves them itself or the SPA does, and neither is proxied.
PREFIX_ORIGIN="the API says it serves"
PREFIXES="$(curl -fsS --max-time 10 "${API}/openapi.json" 2>/dev/null | python3 -c '
import sys, json

NOT_PROXIED = {"health", "openapi.json", "docs", "redoc"}
prefixes = {
    segment[0]
    for path in json.load(sys.stdin)["paths"]
    if (segment := [part for part in path.split("/") if part])
    and not segment[0].startswith("{")
}
print(" ".join(sorted(prefixes - NOT_PROXIED)))
' 2>/dev/null || true)"
if [ -z "${PREFIXES}" ]; then
  # The fallback is a written-down list, which is the thing the paragraph above refuses to
  # rely on — so it is used and announced rather than used quietly. The probes below are
  # still worth running against it; what is not known is whether the API has grown a
  # thirteenth router since somebody last edited this line.
  PREFIXES="auth labels documents search query tenant roles groups system analytics llm-config users"
  PREFIX_ORIGIN="this file lists"
  warn "could not read ${API}/openapi.json, so the prefixes probed below are this file's own copy of the route table and may be one router behind the API"
fi

# **Twelve probes, and deliberately not twelve lines.** A line per prefix spends a third of
# this report saying "yes" twelve times and pushes the answer check off the screen of
# somebody reading it minutes before a room. What an operator needs from this section is one
# of two facts — the proxy list is complete, or these names are missing — so the pass is one
# line carrying the count, and each failing *class* is one line naming the prefixes in it.
#
# Grouped by class rather than by prefix because the remedy belongs to the class and not to
# the prefix: every SPA fall-through is fixed by editing the same two files, and every proxy
# error page by starting the same container. One missing word in nginx's regex would
# otherwise produce twelve failures and a summary line reading `NOT READY — 12 failure(s)`,
# which describes one problem as twelve.
count() { printf '%s' "$#"; }
REACHED=""; FELL_THROUGH=""; PROXY_ERROR=""; SILENT=""; CODES=""
for prefix in ${PREFIXES}; do
  # `--max-time`, for the reason the answer check gives: a proxy that accepts the connection
  # and never replies is a hang, and a check that hangs is one an operator learns to skip.
  REPLY="$(curl -s --max-time 10 -w '\n%{http_code}' "${WEB}/${prefix}" 2>/dev/null || true)"
  CODE="${REPLY##*$'\n'}"
  BODY="${REPLY%$'\n'*}"
  if printf '%s' "${BODY}" | python3 -c 'import sys, json; json.load(sys.stdin)' 2>/dev/null; then
    REACHED="${REACHED} ${prefix}"
    case " ${CODES} " in *" ${CODE} "*) ;; *) CODES="${CODES} ${CODE}" ;; esac
  elif [ -z "${CODE}" ] || [ "${CODE}" = "000" ]; then
    SILENT="${SILENT} ${prefix}"
  else
    case "${BODY}" in
      *"<!doctype"*|*"<!DOCTYPE"*) FELL_THROUGH="${FELL_THROUGH} ${prefix}" ;;
      *)                           PROXY_ERROR="${PROXY_ERROR} ${prefix} (${CODE})" ;;
    esac
  fi
done

PROBED="$(count ${PREFIXES})"
if [ -n "${REACHED}" ]; then
  if [ "$(count ${REACHED})" -eq "${PROBED}" ]; then
    ok "all ${PROBED} prefixes ${PREFIX_ORIGIN} reach the API through the proxy — every one answered in JSON (${CODES# })"
  else
    # Named, unlike the pass above: in a mixed run the interesting half is which ones worked.
    ok "${REACHED# } reach the API through the proxy, answering in JSON (${CODES# })"
  fi
fi
if [ -n "${SILENT}" ]; then
  bad "nothing answered at ${WEB} at all for${SILENT}, so the proxy list was never asked: the frontend container is not serving, or ZENITH_WEB names the wrong port"
fi
if [ -n "${FELL_THROUGH}" ]; then
  bad "${FELL_THROUGH# } fall(s) through to the SPA — missing from the nginx proxy list in docker/nginx.frontend.conf, and from frontend/vite.config.ts with it"
fi
if [ -n "${PROXY_ERROR}" ]; then
  bad "${PROXY_ERROR# } — ${WEB} answered with that status and not in JSON, so this is the proxy's own error page: nginx matched the prefix and could not reach the API behind it"
fi

# --- a real question ----------------------------------------------------------------------
if [ -z "${EMAIL}" ] || [ -z "${PASSWORD}" ]; then
  warn "no credentials given — skipping the search, answer and corpus-visibility checks (pass email and password to run them)"
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
#
# **`active`, not "anything but purged".** The lifecycle is
# `active <-> suspended -> purging -> purged` (`features/tenancy/model.py`), and only the
# first of those four is a corpus anybody can search: `AuthService` refuses the login of an
# account in a suspended organisation and every route but `/system` with it, and a `purging`
# organisation is having its rows deleted underneath the count. Excluding only the tombstone
# meant a suspended organisation's documents were reported as `corpus: 42 documents ready`,
# which is the one number in this script an operator reads as "there is something to
# demonstrate". The leftover-organisations query below has always said `= 'active'`; this one
# now agrees with it.
#
# **And "no rows" is not "could not ask".** `|| true` swallowed psql's exit status, so an
# empty result was the only evidence left and both outcomes printed `could not read the
# corpus`: a `db` container that is not answering, and an installation that holds no
# documents at all. Both are failures — a demonstration needs a corpus — but the first is
# fixed by starting a container and the second by uploading something, and a message that
# cannot tell an operator which sends them to the wrong one. The exit status is now kept and
# read, which is the only thing that distinguishes them.
STATUSES="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" -tAc \
  "SELECT d.status, count(*), t.name
     FROM documents d JOIN tenants t ON t.id = d.tenant_id
    WHERE t.status = 'active'
    GROUP BY d.status, t.name
    ORDER BY count(*) DESC" 2>/dev/null)"
ASKED=$?
if [ "${ASKED}" -ne 0 ]; then
  bad "could not read the corpus — psql exited ${ASKED}: the db container is not answering, or POSTGRES_USER/POSTGRES_DB do not name this installation"
elif [ -z "${STATUSES}" ]; then
  bad "the corpus is empty — no active organisation holds a single document, so there is nothing to search in the room"
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

# --- and the same corpus, counted as the account that will be demonstrated ------------------
#
# **Everything above this line was counted by the database owner, and the owner bypasses
# every RLS policy.** `42 documents ready` is what the tables hold. It is not what
# `demo@example.com` can retrieve, and invariant 1 makes the difference silent on purpose:
# a forgotten filter returns *nothing* rather than everything, so a label the demonstrating
# account's groups do not reach, or a group membership nobody renewed, produces an empty
# screen and no error anywhere. Every line in this report stays green and the room is empty.
#
# So it is counted twice, and **neither count replaces the other**: the owner's says
# ingestion worked, this one says the access control lets somebody see the result. They are
# different questions and the demonstration depends on both.
#
# **When they disagree that is the most interesting line in this report**, and it is a
# failure rather than a warning. A corpus that exists and cannot be reached is the one state
# that looks healthy from every other angle here — the files are on disk, the rows are in the
# table, `document files` passes, ingestion passes — and the first evidence anybody gets is a
# blank document list in front of an audience.
#
# Through `GET /documents` rather than by setting the tenant GUCs in psql by hand. The point
# is to walk the path the client walks: the token, the profile it resolves to, the groups
# that profile carries and the labels those groups reach. Reproducing that in SQL would be
# reproducing the thing under test.
if [ -n "${TOKEN}" ]; then
  # Paged, because a page is all this endpoint has: `MAX_LIMIT` is 200 and there is no
  # total — `DocumentPage`'s docstring says why, and counting under RLS is exactly the cost
  # it declines to pay. So the count is the client's own count, taken the client's own way.
  #
  # urllib rather than curl, alone in this file: following an opaque cursor is a loop that
  # has to carry state between iterations, and ten lines of Python that do it in one place
  # are easier to read — and to be sure of — than a shell loop reassembling a query string.
  RETRIEVED="$(python3 -c '
import json, sys, urllib.request

api, token = sys.argv[1], sys.argv[2]

count, cursor = 0, None
# A bound rather than `while True`. 200 pages of 200 is 40,000 documents, well past the
# 5,000 per tenant the configuration permits, so reaching it means the cursor stopped
# advancing — and an unfinished count is said to be unfinished rather than reported.
for _ in range(200):
    url = api + "/documents?status=ready&limit=200" + (("&cursor=" + cursor) if cursor else "")
    ask = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(ask, timeout=20) as response:
            page = json.load(response)
    except Exception as error:
        print("UNREADABLE " + str(error))
        sys.exit(0)
    count += len(page.get("items") or [])
    cursor = page.get("next_cursor")
    if not cursor:
        print("COUNT " + str(count))
        sys.exit(0)
print("TRUNCATED " + str(count))
' "${API}" "${TOKEN}" 2>/dev/null || printf "UNREADABLE the document list could not be read at all\n")"

  # The owner's count for *this account's* organisation, not the sum printed above: the
  # comparison is only meaningful inside one tenant, and an installation holding two corpora
  # would otherwise always disagree with itself.
  #
  # `:'email'` rather than the address interpolated into the string. psql quotes the variable
  # as a literal itself, so an address containing a quote is a value and never syntax — this
  # is an operator's own argument rather than a stranger's, but a readiness check has no
  # business being the one place in the repository that builds SQL by concatenation.
  #
  # Fed on standard input (`-f -`) and not with `-c`, which is what makes that possible:
  # psql substitutes its variables while parsing a script and hands a `-c` string to the
  # server untouched, so `:'email'` inside `-c` reaches Postgres verbatim and is a syntax
  # error at the colon.
  HELD="$(printf '%s' \
    "SELECT count(*)
       FROM documents d JOIN users u ON u.tenant_id = d.tenant_id
      WHERE lower(u.email) = lower(:'email') AND d.status = 'ready'" \
    | ${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" \
        -v email="${EMAIL}" -tA -f - 2>/dev/null)"
  ASKED=$?
  KIND="${RETRIEVED%% *}"
  DETAIL="${RETRIEVED#* }"
  case "${KIND}" in
    COUNT)
      if [ "${ASKED}" -ne 0 ] || [ -z "${HELD}" ]; then
        # Half the answer, said as half. The corpus block above has already failed on the
        # same psql, so this is a warning rather than a second failure for one cause.
        warn "corpus: ${EMAIL} retrieves ${DETAIL} ready document(s) through GET /documents, and psql could not say how many its organisation holds — the two counts were not compared"
      elif [ "${DETAIL}" -eq "${HELD}" ]; then
        ok "corpus: ${EMAIL} retrieves all ${HELD} of the ready documents its organisation holds"
      elif [ "${DETAIL}" -lt "${HELD}" ]; then
        bad "corpus: ${EMAIL} retrieves ${DETAIL} of the ${HELD} ready documents its organisation holds — $(( HELD - DETAIL )) are invisible to the account that will be demonstrated. Labels or group membership, not ingestion: the count above is the owner's, and the owner bypasses RLS"
      else
        # Not reachable through RLS, which is why it is worth printing: a session that sees
        # more rows than its own tenant holds would be invariant 1 broken the dangerous way
        # round, and the likelier reading is that one of these two queries is wrong.
        bad "corpus: ${EMAIL} retrieves ${DETAIL} ready documents while its organisation holds ${HELD} — a session cannot see more than its tenant has, so one of these two counts is wrong"
      fi ;;
    TRUNCATED)
      warn "corpus: stopped counting what ${EMAIL} retrieves at ${DETAIL} documents — GET /documents kept handing back a cursor, so it was never compared with what the tables hold" ;;
    *)
      bad "corpus: could not ask GET /documents what ${EMAIL} can retrieve, so the number above is the owner's and nobody has checked the account can see it — ${DETAIL}" ;;
  esac
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
#
# **The same `|| true` as the corpus block, and here it produced a green line rather than a
# confused one.** An unreachable `db` made this query empty, empty was the only evidence, and
# empty read as good news — so `the system panel lists no empty organisations` printed on an
# installation nobody had been able to ask. That is the sentence this whole file argues
# against: a check that reports health when it cannot tell is worse than one that cries wolf,
# because nobody switches it off and nobody looks again.
#
# A warning and not a failure, and for once that is not the block's own leniency doing the
# work: not knowing whether there are leftovers is at most as serious as knowing there are,
# which is a warning by the paragraph above. The cause is not softened either — a `db` that
# cannot answer this query could not answer the corpus one, and that block fails.
LEFTOVERS="$(${COMPOSE} exec -T db psql -U "${POSTGRES_USER:-zenith}" -d "${POSTGRES_DB:-zenith}" -tAc \
  "SELECT t.name FROM tenants t
    WHERE t.status = 'active'
      AND NOT EXISTS (SELECT 1 FROM users u WHERE u.tenant_id = t.id)
      AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.tenant_id = t.id)" 2>/dev/null)"
ASKED=$?
if [ "${ASKED}" -ne 0 ]; then
  warn "could not read the organisation list — psql exited ${ASKED}, so what the system panel will show is unknown rather than empty"
elif [ -z "${LEFTOVERS}" ]; then
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
