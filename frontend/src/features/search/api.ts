/**
 * Passage search: the lexical and dense halves, and what came back from each.
 */

import { request } from "@/shared/api/http";

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  filename: string;
  /** Which viewer opens this passage. See `Citation.media_type`. */
  media_type: string;
  /** `null` for a document with no pages. */
  page_num: number | null;
  char_start: number;
  char_end: number;
  text: string;
  bboxes: Array<Record<string, number>>;
  /** The document's labels, as ids. Names come from `GET /labels`, so an id with no name
      is a compartment this caller reaches the passage through some other label. */
  label_ids: string[];
  lexical_rank: number | null;
  dense_rank: number | null;
  score: number;
  lexical_score: number | null;
  dense_score: number | null;
  rerank_score: number | null;
}

/**
 * How much the corpus has to say about the question, decided by the cross-encoder's score
 * on the best passage.
 *
 * A separate field from `degraded`, and the distinction is load-bearing: `degraded` says a
 * component of ours was missing, this says the corpus was. Rendering them the same way
 * would tell a customer their installation is broken when their archive simply does not
 * cover what they asked.
 */
export type Relevance = "confident" | "weak" | "none" | "not_assessed";
export type RetrievalMode = "legacy" | "hybrid" | "auto" | "direct";

export interface CoverageReceipt {
  version: "evidence-coverage-v1";
  strategy: "direct" | "hybrid";
  coverage_method: "eligible_scope_manifest" | "candidate_set";
  execution_status: "complete" | "partial" | "unavailable" | "canceled";
  eligible_units: number | null;
  selected_units: number;
  assessed_units: number;
  failed_units: number;
  skipped_units: number;
  assessment_windows: number;
  manifest_assessment_complete: boolean;
  snapshot_status: "unchanged" | "changed" | "unknown";
  source_representation: "eligible_parsed_content";
  reason_codes: string[];
}

export interface SearchResult {
  hits: SearchHit[];
  degraded: boolean;
  reason: string | null;
  took_ms: number;
  /** Absent on an older server, and `confident` is the safe reading of silence. */
  relevance?: Relevance;
  requested_judge_provider?: string;
  fallback_provider?: string | null;
  evidence_status?: string | null;
  evidence_policy?: string | null;
  receipt?: CoverageReceipt | null;
}

export interface SearchCapabilities {
  direct_enabled: boolean;
}

export function searchCapabilities(token: string): Promise<SearchCapabilities> {
  return request<SearchCapabilities>("/search/capabilities", token);
}

/**
 * The retrieval mechanism itself, with nothing generated on top — what F7's hybrid search
 * actually returns, ranked, before an LLM ever reads it. Distinct on purpose from asking a
 * question in Chat: this is for confirming *why* a passage ranked where it did, not for an
 * answer in prose.
 */
export function search(
  token: string,
  q: string,
  labels?: string[],
  signal?: AbortSignal,
  mode: RetrievalMode = "legacy",
): Promise<SearchResult> {
  const params = new URLSearchParams({ q });
  for (const label of labels ?? []) params.append("labels", label);
  if (mode !== "legacy") params.set("mode", mode);
  return request<SearchResult>(`/search?${params}`, token, { signal });
}
