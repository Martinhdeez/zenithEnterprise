# PR 07 public claim-support trial: Jev Noul versus citation-only acceptance

Optional strict mode has a working real Jev assessor path. On a small disjoint
SciFact development check with oracle source abstracts, the frozen `0.6` Noul
cutoff accepted 8/10 human-supported claims and 0/10 human-contradicted claims.
The two rejected supported claims are false suppression. These data support
continued experimentation behind the disabled flag, **not** a general live
support-quality qualification or a change to the default answer path.

## Fixed source and protocol

Use the [official AllenAI SciFact repository](https://github.com/allenai/scifact)
and its [data format](https://github.com/allenai/scifact/blob/master/doc/data.md).
The public development claim/evidence annotations are expert-written. The
archive, development claims, and corpus SHA-256 hashes are in the
[manifest](evidence-v3-scifact-support-manifest-2026-09-26.json). The corpus and
raw Jev replies stay in the ignored handoff folder; only case IDs, labels,
scores, provenance, usage, and latency are published in the
[case CSV](evidence-v3-scifact-support-cases-2026-09-26.csv). No tenant source or
private query was sent.

`eval.scifact_support_trial` selects annotated claim/document pairs in SHA-256
order. It sends the entire corresponding title and abstract, one claim per
request, through the production Jev adapter with only the `claim_support`
purpose enabled. This supplies an oracle document, bypassing retrieval and
generation. The assessor uses Jev `jev-1.13.0`, versioned
`zenith-claim-support-noul-v1` rubric hash
`268504224418847ae38091b5aa08c0da10456fdf24e867d228be5f0904b7ff81`,
and requires validated Noul probability. A failed judgment would be recorded
as unassessed, never as zero support. All 68 responses were valid.

The first 24 SUPPORT and 24 CONTRADICT pairs were development observations at
the configured default `0.8`. After seeing them, `0.6` was chosen and frozen
before the **next** 10+10 SHA-ranked pairs were called. These 20 pairs have no
claim/document overlap with the first 48, but both sets come from the official
development split. This is a disjoint within-development check, not a hidden
test set. The configured product flag remains off and its conservative default
cutoff remains `0.8`; the `0.6` value was a trial cutoff only.

| Sample and cutoff | Supported accepted | Contradicted accepted | Valid assessments |
| --- | ---: | ---: | ---: |
| Development 24+24, `0.8` | 12/24 (50%) | 0/24 (0%) | 48/48 |
| Disjoint check 10+10, frozen `0.6` | 8/10 (80%) | 0/10 (0%) | 20/20 |

An older citation-only binder verifies that marker `[1]` refers to a supplied
passage; with this oracle evidence and syntactically valid `[1]`, it would
accept all 10 supported **and all 10 contradicted** claims in the disjoint
sample. That is a binder counterfactual, not a measured end-to-end run of
upstream main. The new strict route reduces those contradiction accepts in
this trial, at the cost of two false suppressions and extra inference. It
cannot distinguish semantic contradiction from missing evidence with this
binary Noul rubric, so low-score cases are called `insufficient` unless an
independent deterministic check proves a contradiction.

The 68 live requests reported 56,072 input and 1,496 output tokens; the sum
of provider elapsed fields is 19.081 seconds. These are provider-reported
measurements, not sustainable end-to-end throughput or invoiced cost. The
trial consumed 68 calls after 930 earlier approved public calls; one separate
real application-role strict-mode smoke consumed one more, for 999
reserved/used against the 1,000-call authorization. Do not rerun a live trial
without a new budget and purpose review.

## Reproduction and limits

Run the offline case export from `backend/` after obtaining the pinned public
SciFact files and saved local raw results:

```powershell
uv run python -m eval.scifact_support_export --dev ../Zenith_Evidence_Fork_v3/datasets/scifact/claim-live-48.json --locked ../Zenith_Evidence_Fork_v3/datasets/scifact/claim-live-locked-20.json --csv eval/reports/evidence-v3-scifact-support-cases-2026-09-26.csv --manifest eval/reports/evidence-v3-scifact-support-manifest-2026-09-26.json
```

`uv run python -m eval.scifact_support_trial --claims PATH --corpus PATH
--ledger PATH --output PATH --preflight` verifies local file pins and planned
cases without a Jev call. Live mode additionally requires an ephemeral
credential, the `claim_support` egress purpose, and `--allow-public-live`.
The reservation ledger prevents accidental replay from being counted as free.

SciFact annotations may contain multiple alternative rationale sets; this
trial uses one annotated document and the full abstract. It does not measure
retrieval recall, citation span fidelity, claim decomposition, generator repair,
exception retention, or a complete answer. The 10+10 check is too small to
establish a deployment error bound. A larger held-out, reviewed-label study,
matched main-vs-strict generated answers, and actual cost/latency measurements
are required before promoting this optional mode.
