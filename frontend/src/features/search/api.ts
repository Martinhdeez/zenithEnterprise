/**
 * Passage search: the lexical and dense halves, and what came back from each.
 */

import { request } from "@/shared/api/http";

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  filename: string;
  page_num: number;
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

export interface SearchResult {
  hits: SearchHit[];
  degraded: boolean;
  reason: string | null;
  took_ms: number;
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
): Promise<SearchResult> {
  const params = new URLSearchParams({ q });
  for (const label of labels ?? []) params.append("labels", label);
  return request<SearchResult>(`/search?${params}`, token, { signal });
}
