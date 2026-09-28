# Preregistered QASPER remaining-paper extension

This protocol was recorded after candidate preparation and **before any new Jev
dispatch or inspection of the new cohort's human answer labels**. It extends the
previous fixed-candidate English QASPER test with the 176 source-bearing test
papers not selected in the historical 240-paper run. The previous predictions
are reused without replay. The new cohort is reported separately; the 416-paper
join is descriptive because the first 240 labels have already been seen.

- Public QASPER v0.3 test JSON SHA-256:
  `6e29ad410e6e39aa1936017fb965b30a20eb2e7751997f55b97c9d281aa884e5`.
- Historical 240-paper frozen manifest SHA-256:
  `083ef6880efaac1307c6bedd3ab3b29d3038132370780cf54bce1610aa2a26c7`.
- New 176-paper manifest SHA-256:
  `2d5f23e9a955eca1f6cf19d65140e90e4aabf33631d6acb1e9e69e3bea0160b4`.
- Selection: exclude the historical 240 paper IDs, then SHA-256 sort the
  remaining public source-bearing paper IDs and select the lowest SHA-256
  question ID within each paper. `prepare` projects away all answer annotations.
  The two cohorts have zero overlapping paper IDs.
- Candidate and scoring policy: exactly the historical QASPER method in
  `eval/qasper_heldout_jev.py`: BGE-M3 dense top eight of current-main chunks,
  identical frozen candidates for local TEI and Jev Noul, 1,100 rendered BGE
  token limit, complete accepted human evidence as the primary measure, and
  binary nDCG@8 restricted to evidence-addressable candidate-hit cases. Failed
  Jev assessment falls back to the frozen TEI order. Use 2,000 paired paper
  bootstrap draws with seed 1729; report denominators and failures explicitly.
- Pinned model/rubric: local TEI
  `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1@1427fd652930e4ba29e8149678df786c240d8825`;
  Jev `jev-1.13.0`; Noul rubric `zenith-contribution-noul-v1` with SHA-256
  `ea794cf80e72da58ace8bae07b33ec9164aed4e274a5a941ced3329d2c625dac`.
- User authorization: at most 1,408 new calls and 1 USD additional. Frozen
  manifest plans **1,396** calls. Its 9,869,151 conservatively reserved input
  tokens imply about **$0.415** at the published $0.042/million-input-token
  rate; the runtime also caps reservations at 20 million tokens, or $0.84 at
  that rate. These are estimates, not provider invoices. Stop if a cap is met.

The manifest, ledger, raw public source, credentials, and provider responses
remain ignored and local. The live runner requires the exact SHA above, the
purpose-specific reranking flag, and an explicit live flag. It reserves each
paper before dispatch and never replays an uncertain partial batch.

This task tests fixed-candidate evidence ranking. It does not qualify generated
answers, the Score formulation, segmentation, packets, strict support, private
enterprise data, sustained provider throughput, or a production default change.
