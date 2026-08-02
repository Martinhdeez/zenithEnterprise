# F5 validation — empirical results

Closes Gaps 2 and 4 from `2026-08-02-f5-decisions.md`. Predictions were committed first, in
`2026-08-02-f5-validation-predictions.md`, before either measurement ran.

Two predictions were wrong. Both are more useful than the ones that were right.

---

## Gap 2 — the two-column detector, against the M0 corpus

**Where it ran:** the development Mac. Pure pdfplumber and arithmetic; no models, no
containers. Layout is a property of the PDF, not of the hardware.

### Result: PASS, after two rewrites the corpus forced

| Version | IRS instructions | IRS pub-15 | BERT (two-column) | False positives |
|---|---|---|---|---|
| strip no word crosses | **0%** | 0% | 0% | 0% |
| bins each word spans | 59% | 3% | 0% | 0.3% |
| **bins of word centres, dense pages only** | **79%** | **97%** | **100%** | **1.0%** |

The first version — the one shipped in the original F5 commit — fired on **zero of 1,842
pages**. It looked for a vertical strip no word crossed, and no such strip exists on a real
page: a header, a page number or a figure caption spans the full width and closes it. The
unit tests passed the whole time, because the synthetic fixture had no header.

The version that works bins **word centres** rather than the bins each word spans, and only
judges pages above 250 words. That second condition matters as much as the first: a title
page or a page of equations has empty bins from sparsity rather than layout, and judging
those produced a 73% false-positive rate on a single-column paper.

### Against the committed thresholds

| Criterion | Threshold | Measured |
|---|---|---|
| two-column pages flagged | ≥ 70% | **100%** (16/16) |
| single-column pages flagged | ≤ 5% | **1.0%** (16/1,574) |
| IRS pages inspected by hand | 5 of 5 interleaved | **5 of 5** |

The hand inspection is the part no metric could do. From `irs-pub-15` page 2:

> f one is submitted by the em- base limit is $184,500. ployee, and the federal income tax
> withholding proce- The Medicare tax rate is 1.45% each for the employee dures in Pub. 15-T

Two columns spliced line by line. Every word correct, every character present, the meaning
destroyed — exactly the failure M0 identified and exactly the one no character count sees.

### Prediction 1 was wrong, for an instructive reason

I predicted arXiv papers would be 85–95% flagged. They came in at 0%, and the detector was
right: **NeurIPS format is single-column.** `attention-is-all-you-need` and `rag-paper` were
mislabelled in my own ground-truth table. `nasa-scanned-report` was mislabelled too — M0 had
already found it *has* an OCR layer, just a poor one, and I recorded it as having none.

Two of fourteen ground-truth labels were wrong before a single measurement ran. The
corrected table is in `eval/layout.py`.

### Residual false positives

`boe-monetary-policy` 11%, `nasa-technical-report` 8%. Both are report formats with
side-by-side charts and captions. A false positive costs one warning on a page that is
fine; a false negative puts interleaved text in the index where nothing can see it. The
threshold leans towards the cheaper mistake deliberately.

Reproduce with `python -m eval layout`.

---

## Gap 4 — real TEI on the hardware M0 broke

**Where it ran:** the VPS — 4 cores, 7.6 GB total, x86_64, `ZENITH_HARDWARE=low-spec`, TEI
`cpu-1.8` serving BGE-M3 with `--max-batch-tokens 2048 --max-client-batch-size 4`. The same
machine and the same image that produced all three M0 failures.

**Method.** Our own modules — `routing`, `chunker`, `TeiClient` — driven over a real PDF, not
a reimplementation. The database write path is deliberately excluded: all three failures
live in the parse-chunk-embed leg, and adding Postgres would have broadened what the test
could break on without touching what it proves.

### Results

| | `gdpr.pdf` | `irs-1040-instructions.pdf` |
|---|---|---|
| Pages | 88 | 126 |
| Chunks | 419 | 691 |
| Requests to TEI | 105 | 173 |
| **HTTP 413** | **0** | **0** |
| Largest batch | 4 items, 1,522 est. tokens | 4 items, 1,592 est. tokens |
| **Peak concurrent requests** | **1** | **1** |
| **Peak container memory** | **2.55 GiB** | **2.67 GiB** |
| Vectors returned | 419 × 1024 | 691 × 1024 |
| Wall time | 6m 28s | 16m 53s |

Against the committed thresholds: **zero 413s** (predicted 0), **peak memory well under
4.5 GB** (predicted 3.5–4.2, measured 2.55–2.67), **at most one request in flight**
(predicted ≤ 1).

The largest batch reached 1,592 estimated tokens against a 2,048 budget. That headroom is
the mitigation working on real text: the batcher stopped short of the ceiling on its own,
with nobody tuning anything.

### Caveat on the memory figure, stated because it changes how much the number is worth

Peak memory is **sampled** every 250 ms by polling `docker stats`, not read from a
high-water mark. A spike between two samples is invisible. The reported peak is therefore a
**lower bound on the truth**, not the truth.

It is good enough for the question asked — 2.67 GiB against a 6.59 GiB kill and a machine
with roughly 5 GB free is not a close call — and it would not be good enough to certify a
tighter margin. Reading the cgroup's own `memory.peak` would fix it and is the right thing
to do if this is ever re-run for a narrower question.

### Prediction 3 was wrong, and it changes the hardware specification

Predicted 4–8 minutes per 100 pages. Measured **7.35 on GDPR** and **13.4 on the IRS
document** — outside the range, and the miss is the interesting part.

Per *chunk*, the two runs agree closely: 0.88 s and 1.39 s. Per *page* they do not, because
a dense page produces more and fuller chunks. **"Minutes per 100 pages" is the wrong unit.**
It was M0's unit, and it silently assumes an average text density that a tax form does not
have.

The consequence for the customer-facing number is direct. M0's specification says
**10,000 pages ≈ 8 hours**, derived from ~5 min/100 pages. On dense documents the same
corpus is **closer to 22 hours**. The published figure needs a range and a stated
assumption, not a single number.

That correction belongs in `technical-decisions.md` §11b and `mvp.md` §8.

### Negative control: is omitting `--max-concurrent-requests` load-bearing?

The compose file omits the flag and the comment beside it says the flag panics TEI. Without
a reproduction that is folklore, and folklore gets tidied away by the next person.

TEI was restarted **with** `--max-concurrent-requests 4` and driven with the same load.

**It died. Exit 139**, with the panic M0 recorded, verbatim:

```
thread 'tokio-runtime-worker' panicked at core/src/queue.rs:87:14:
Queue background task dropped the receiver or the receiver is too behind.
This is a bug.: "Full(..)"
```

It was run twice, and the second run is the one that settles the question.

| | Run 1 | Run 2 |
|---|---|---|
| Successful requests before death | ~20 | **3** |
| Phase it died in | ambiguous | **sequential** |
| Exit code | 139 | 139 |

Run 2 is decisive: `sequential outcomes: {'200': 3, 'RemoteProtocolError': 1,
'ReadError': 21, 'ConnectError': 15}`. The server died after **three** requests, sent one at
a time, by the same client we ship. It never reached the concurrent phase alive.

**Prediction 4 was right and my hedge was wrong.** I predicted death within the first two
dozen requests, then hedged: *"a strictly sequential client may never trigger it… if it
survives, the honest conclusion is that our sequential client is what avoids the panic."*
It did not survive. The flag is fatal on `cpu-1.8` regardless of how carefully the client
behaves — sequential requests are enough.

So the compose comment is correct exactly as written, and stronger than I was prepared to
claim: **omitting `--max-concurrent-requests` is not a mitigation layered on top of our
sequential client, it is the only thing standing between this deployment and a server that
dies after three requests.** Our sequential client protects against a different failure —
memory — and does not protect against this one at all.

The number of requests before death varies between runs (3 and ~20), which is what a race
in a bounded queue looks like. That variance is itself worth recording: a customer could see
this survive a smoke test and die in production the same afternoon.

---

## What this validation did not cover

- **The database write path** under a real TEI. Chunks and embeddings were produced but not
  persisted in this run; that path is covered by the pipeline tests against real Postgres.
- **Concurrent ingestion of several documents.** `low-spec` sets concurrency to 1 and the
  measurement respects it. Nothing here proves what two workers would do, because nothing
  is supposed to run two.
- **The `gpu` and `cpu` profiles.** Both remain unmeasured. `gpu` in particular is inherited
  from upstream defaults and is still marked as carrying no evidence.
