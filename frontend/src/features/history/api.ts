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

/**
 * Past questions. Whose, is decided by the server from the caller's permissions — there is
 * deliberately no scope parameter, because that would be the client asking rather than the
 * server deciding.
 */
export function history(token: string, cursor?: string | null): Promise<HistoryPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return request<HistoryPage>(`/query/history${query}`, token);
}
