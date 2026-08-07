/**
 * Labels: the tenant's own vocabulary, and the access-control primitive RLS reads.
 *
 * Every function here can change who sees what — which is why `mergeLabels` reports its
 * consequences rather than just performing them.
 */

import { request } from "@/shared/api/http";

export interface Label {
  id: string;
  name: string;
  is_default: boolean;
}

/**
 * Labels the caller may file a document under — the same set `/documents` will accept in
 * `label_ids`, and no larger. Fetched at upload time rather than derived from `Folders`'
 * tree: a label with zero documents so far exists here and nowhere else, and a brand-new
 * tenant needs to be able to upload its very first document under one.
 */
export function labels(token: string): Promise<Label[]> {
  return request<Label[]>("/labels", token);
}

/**
 * A tenant's labels are not a fixed set this product ships with — the whole point of F3 was
 * that access control follows whatever an organisation actually calls its own document
 * categories. Gated server-side on `labels.manage`; a caller without it gets the same
 * `ApiError` shape every other refusal here does, which is why this doesn't special-case
 * the 403.
 */
export function createLabel(token: string, name: string): Promise<Label> {
  return request<Label>("/labels", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function deleteLabel(token: string, labelId: string): Promise<void> {
  return request<void>(`/labels/${labelId}`, token, { method: "DELETE" });
}

export interface LabelSearchItem extends Label {
  /**
   * When this label was last applied to a document the caller can see, or null if never.
   * RLS-scoped like `documents` — it describes the corpus you can open, not the tenant's.
   */
  last_used: string | null;
  /**
   * Documents *this caller can see* carrying the label — not a tenant-wide total.
   * `document_labels` inherits its RLS policy from `documents`, so an administrator who
   * does not reach a label is told how many of its documents they could open, never how
   * large the compartment is. Displaying it as "N documents" without that caveat is fine;
   * displaying it as a tenant statistic is not.
   */
  documents: number;
}

export interface LabelSearchPage {
  items: LabelSearchItem[];
  next_cursor: string | null;
}

export type LabelSort = "name" | "usage_count" | "created_at" | "last_used";

/**
 * Paginated label search. `cursor` is opaque and belongs to the `sort` that produced it —
 * the server rejects one issued under a different ordering rather than silently returning
 * a page from the wrong position, so a sort change has to restart from no cursor.
 */
export function searchLabels(
  token: string,
  options: {
    q?: string;
    sort?: LabelSort;
    cursor?: string;
    limit?: number;
    /** Drop labels no document carries. */
    inUse?: boolean;
    /** Narrow to labels on documents this caller uploaded. */
    mine?: boolean;
  } = {},
): Promise<LabelSearchPage> {
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.sort) query.set("sort", options.sort);
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.limit) query.set("limit", String(options.limit));
  if (options.inUse) query.set("in_use", "true");
  if (options.mine) query.set("mine", "true");
  const suffix = query.toString();
  return request<LabelSearchPage>(`/labels/search${suffix ? `?${suffix}` : ""}`, token);
}

export interface LabelMergeResult {
  target: Label;
  merged: string[];
  documents_relabelled: number;
  /**
   * Documents that become visible to at least one role that cannot see them today.
   *
   * The number the confirmation step exists to show. It counts both directions a merge
   * moves access — documents that gain a more widely held label, and documents that never
   * moved but whose label a role newly holds — so a zero here is a real "this exposes
   * nothing", not "nothing was relabelled".
   */
  visibility_widening: number;
  dry_run: boolean;
}

/**
 * Fold labels together. Not a rename: labels are the access-control primitive, so this
 * moves documents between roles.
 *
 * Two-step by contract, not by convention. `dry_run: true` reports what would happen and
 * changes nothing; the real call is refused with a 409 if it would widen visibility and
 * `acknowledge_widening` is not set. A client that skips the preview cannot merge past
 * that refusal by accident.
 */
export function mergeLabels(
  token: string,
  body: {
    sources: string[];
    target: string;
    dry_run?: boolean;
    acknowledge_widening?: boolean;
  },
): Promise<LabelMergeResult> {
  return request<LabelMergeResult>("/labels/merge", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
