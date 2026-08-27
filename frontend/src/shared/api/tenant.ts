/**
 * What the installation currently holds: how many documents, in what state, and which
 * components are answering.
 *
 * Shared rather than owned by `documents`: the shell reads it to decide whether search is
 * worth offering at all, and the document screens read it for their counters. A feature
 * that owned it would be imported by the shell for a reason unrelated to that feature.
 */

import { request } from "@/shared/api/http";

/**
 * The document statuses that mean "still being worked on".
 *
 * Mirrors `DOCUMENT_STATUSES` in the backend model, and it is a real duplication rather
 * than a convenience: the client cannot import Python. `documents` is keyed by whatever
 * the server sends, so a typo here is not a type error — it is a counter that reads zero
 * forever. Which is exactly what happened before F16 noticed.
 */
export const IN_FLIGHT = [
  "pending",
  "parsing",
  "chunking",
  "embedding",
  // Filing, after the chunks are committed. A document here is already searchable by
  // whoever reaches its labels; what is undecided is which labels those will be.
  "classifying",
] as const;

export interface TenantStatus {
  documents: Record<string, number>;
  chunks: number;
  hardware: string;
  components: { embeddings: boolean; reranker: boolean; generation: boolean };
  searchable: boolean;
}

export function tenantStatus(token: string): Promise<TenantStatus> {
  return request<TenantStatus>("/tenant/status", token);
}

/**
 * One retrieved passage, exactly as `GET /search` scored and ranked it — no generation
 * involved. `lexical_rank` and `dense_rank` are positions and either may be `null`; the
 * `*_score` fields are the magnitudes behind them, kept apart because a lexical score and a
 * dense score are never comparable to each other.
 */
