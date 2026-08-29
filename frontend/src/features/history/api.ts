/**
 * Past questions. The record that outlives a tab.
 */

import { request } from "@/shared/api/http";

export interface HistoryEntry {
  query_id: string;
  question: string;
  answer: string | null;
  model_used: string | null;
  citations: number;
  latency_retrieval_ms: number | null;
  latency_generation_ms: number | null;
  created_at: string;
  mine: boolean;
}

export interface HistoryPage {
  entries: HistoryEntry[];
  next_cursor: string | null;
}

export interface HistoryFilters {
  /** Matched against the question text only — never the answer. */
  search?: string;
  /** Only your own, for somebody who may read everyone's. */
  mine?: boolean;
  /** Only questions no document answered. */
  unanswered?: boolean;
}

/**
 * Past questions. Whose, is decided by the server from the caller's permissions — there is
 * deliberately no scope parameter, because that would be the client asking rather than the
 * server deciding.
 *
 * The filters below only ever narrow that set. `mine` is somebody who may read everyone's
 * asking to look away from it; there is no parameter for the opposite.
 */
export function history(
  token: string,
  cursor?: string | null,
  filters: HistoryFilters = {},
): Promise<HistoryPage> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (filters.search?.trim()) params.set("search", filters.search.trim());
  // Sent only when true. `mine=false` and an absent `mine` mean the same thing to the
  // server, and a query string that says so on every request is noise in every log.
  if (filters.mine) params.set("mine", "true");
  if (filters.unanswered) params.set("unanswered", "true");
  const query = params.toString();
  return request<HistoryPage>(`/query/history${query ? `?${query}` : ""}`, token);
}
