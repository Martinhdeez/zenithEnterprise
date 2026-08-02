# F5 validation — predictions, recorded before measuring

Committed before either validation runs. M0 established the discipline and then justified
it: the headline recall prediction was right (70–80% predicted, 75% measured) and the table
prediction was badly wrong (0–20% predicted, 80% measured), and the second was worth more
than the first. A prediction only means something if it is possible to be publicly wrong.

---

## Gap 2 — the two-column detector against the M0 corpus

The corpus gives ground truth for free: three arXiv papers that are genuinely two-column
throughout, four EU regulations that are single-column throughout, three IRS documents that
are two-column and tabular, and one scan with no text layer.

### Pass criteria, fixed now

| Criterion | Threshold |
|---|---|
| arXiv pages flagged `LAYOUT` | **≥ 70%** |
| EU regulation pages flagged `LAYOUT` (false positives) | **≤ 5%** |
| `nasa-scanned-report.pdf` and `image-only.pdf` | `UNREADABLE` without OCR |
| IRS pages flagged, inspected by hand | **5 of 5** show real interleaving |

### Predictions

1. **arXiv: 85–95% flagged.** Two-column bodies throughout; the misses will be the title
   page, the references, and any full-width figure.
2. **EU regulations: 0–3% flagged.** These are single-column with wide margins. The margin
   is the risk — a wide left margin beside a numbered list could look like a gutter, which
   is exactly why the detector requires substantial text on *both* sides.
3. **IRS: 40–70% flagged.** Lower than arXiv, because form pages are tabular rather than
   two-column, and a table's narrow columns should not produce one wide gutter. If it comes
   in near 100%, the detector is firing on tables and the reason it fires is not the reason
   I designed it for.
4. **The spacing detector fires on arXiv, not on the regulations.** M0 found
   `densevectorindexofWikipedia` on arXiv pages specifically.
5. **The most likely failure is prediction 2.** If false positives exceed 5%, `COLUMN_GAP`
   is too small.

---

## Gap 4 — real TEI on the VPS, `low-spec` profile

The code encodes three M0 lessons. None of them has been observed holding.

### Pass criteria, fixed now

| Criterion | Threshold |
|---|---|
| HTTP 413 responses during a dense ingest | **exactly 0** |
| TEI container peak memory | **< 4.5 GB** |
| Concurrent in-flight requests to TEI | **≤ 1** |
| Throughput | within **2×** of M0's ~5 min / 100 pages |
| Negative control: TEI started **with** `--max-concurrent-requests 4` | **dies** (exit 139 or OOM) |

### Predictions

1. **Zero 413s.** The token budget is the whole point, and the estimate is deliberately
   pessimistic. Any 413 means the estimate is optimistic somewhere.
2. **Peak memory 3.5–4.2 GB.** M0 measured 3.71 GB at rest with these flags; ingestion adds
   activations for one batch of at most four short chunks.
3. **Throughput 4–8 minutes per 100 pages**, so 100 dense IRS pages in under fifteen. Slower
   than M0's figure is expected: this run parses as well as embeds.
4. **The negative control dies within the first two dozen requests.** Least certain
   prediction here — M0 saw the panic under concurrent load, and a strictly sequential
   client may never trigger it even with the flag present. **If it survives, the honest
   conclusion is that our sequential client is what avoids the panic, and the compose
   comment should say that instead of blaming the flag alone.**
5. **The likeliest surprise is memory.** Peak is sampled from the cgroup, and if the sample
   interval misses a spike the number will look better than the truth.
