/**
 * Everything that is not the stream: login, upload, status.
 *
 * Relative paths throughout. The bundle is served next to the API inside the customer's
 * network, so a baked-in host would be a value that is wrong on every installation except
 * the one it was built for — and an on-premise product is only ever installed somewhere
 * else.
 */

/**
 * The document statuses that mean "still being worked on".
 *
 * Mirrors `DOCUMENT_STATUSES` in the backend model, and it is a real duplication rather
 * than a convenience: the client cannot import Python. `documents` is keyed by whatever
 * the server sends, so a typo here is not a type error — it is a counter that reads zero
 * forever. Which is exactly what happened before F16 noticed.
 */
export const IN_FLIGHT = ["pending", "parsing", "chunking", "embedding"] as const;

export interface TenantStatus {
  documents: Record<string, number>;
  chunks: number;
  hardware: string;
  components: { embeddings: boolean; reranker: boolean; generation: boolean };
  searchable: boolean;
}

/**
 * RFC 7807 Problem Details, as this API returns them.
 *
 * `code` and `message` are retained by the server alongside the standard members, so this
 * client keeps reading them: `code` is the stable token to switch on, and `detail` and
 * `message` carry the same string. Preferring `detail` with `message` as the fallback means
 * this client works against both the current server and any older one still deployed.
 */
export interface ApiErrorBody {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code: string;
  message: string;
  /** Present on 429. Seconds until the caller may retry. */
  retry_after?: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
  });
  if (!response.ok) {
    // Every error in this API has the same `{code, message}` shape — that was the point of
    // F10's handler work — so one branch covers all of them. `code` is what the UI
    // switches on; `message` is what it may show.
    const body: ApiErrorBody = await response
      .json()
      .catch(() => ({ code: "unknown", message: "The request failed." }));
    throw new ApiError(response.status, body.code, body.detail ?? body.message);
  }
  // `204 No Content` has no body — `delete_document` answers with one — and `.json()` on an
  // empty stream throws `SyntaxError: Unexpected end of JSON input` rather than returning
  // `undefined`. Nothing called through this path until deletion did, so it went unnoticed.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export function login(email: string, password: string): Promise<TokenPair> {
  return fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  }).then(async (response) => {
    if (!response.ok) {
      const body: ApiErrorBody = await response
        .json()
        .catch(() => ({ code: "unknown", message: "Sign in failed." }));
      // Deliberately not distinguishing "no such user" from "wrong password" in the UI
      // either. The API refuses to, because telling them apart turns a login form into an
      // account-enumeration tool, and a helpful client would undo that.
      throw new ApiError(response.status, body.code, body.detail ?? body.message);
    }
    return response.json() as Promise<TokenPair>;
  });
}

/**
 * Trade the refresh token for a new pair, silently, before the access token expires.
 *
 * The server rotates the refresh token on every call — the response's `refresh_token` is
 * never the one that was sent — so the caller must persist both fields, not just the
 * access token. Keeping the access token at its short, deliberate lifetime (15 minutes)
 * while the *session* lasts as long as the refresh token (14 days) is the point: shortening
 * that by only ever storing the access token, as this client did until now, was a bug, not
 * a security margin — nobody chose 15-minute sessions on purpose.
 */
export function refreshTokens(refreshToken: string): Promise<TokenPair> {
  return fetch("/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  }).then(async (response) => {
    if (!response.ok) {
      const body: ApiErrorBody = await response
        .json()
        .catch(() => ({ code: "unknown", message: "Session expired." }));
      throw new ApiError(response.status, body.code, body.detail ?? body.message);
    }
    return response.json() as Promise<TokenPair>;
  });
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
export interface SearchHit {
  chunk_id: string;
  document_id: string;
  filename: string;
  page_num: number;
  text: string;
  bboxes: Array<Record<string, number>>;
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

export interface DocumentSummary {
  id: string;
  filename: string;
  description: string | null;
  sha256: string;
  status: string;
  status_detail: string | null;
  page_count: number | null;
  size_bytes: number;
  uploaded_by: string | null;
  created_at: string;
}

/**
 * What `POST /documents` actually answers with — not the document alone.
 *
 * `deduplicated` and `labels` were part of the wire contract from the start (see the
 * backend's `UploadResponse` docstring) and this client was declaring a flatter shape that
 * didn't match it: `uploaded.id` read `undefined` on every upload, silently, because nothing
 * here ever threw — `fetch`+`.json()` doesn't care that the shape it parsed isn't the shape
 * a caller expected. The "recent uploads" list in `Upload.tsx` had been rendering blank rows
 * since the day it was written.
 */
export interface UploadResponse {
  document: DocumentSummary;
  labels: string[];
  deduplicated: boolean;
}

export function uploadDocument(
  file: File,
  token: string,
  labels?: string[],
  options?: { filename?: string; description?: string },
): Promise<UploadResponse> {
  const body = new FormData();
  body.append("file", file);
  for (const label of labels ?? []) body.append("labels", label);
  // Both optional overrides. Sent only when there's something to send: an empty string
  // would tell the server "rename this to nothing" instead of "use the file's own name".
  if (options?.filename) body.append("filename", options.filename);
  if (options?.description) body.append("description", options.description);
  // No `Content-Type` header: the browser must set it, because only the browser knows the
  // multipart boundary it generated. Setting it by hand produces a body the server cannot
  // parse, and the error says nothing about why.
  return request<UploadResponse>("/documents", token, { method: "POST", body });
}

export interface DocumentPage {
  items: DocumentSummary[];
  next_cursor: string | null;
}

/**
 * One page of the corpus the caller's labels reach — newest first, optionally narrowed to
 * one folder from `Folders`. `filter.labelId === null` means the *unlabelled* folder
 * specifically, not "no filter" — omit `filter` entirely for that.
 */
export function listDocuments(
  token: string,
  cursor?: string | null,
  filter?: { labelId: string | null } | null,
): Promise<DocumentPage> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (filter) {
    if (filter.labelId === null) params.set("unlabelled", "true");
    else params.set("label_id", filter.labelId);
  }
  const query = params.toString();
  return request<DocumentPage>(`/documents${query ? `?${query}` : ""}`, token);
}

export function deleteDocument(token: string, documentId: string): Promise<void> {
  return request<void>(`/documents/${documentId}`, token, { method: "DELETE" });
}


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

export type LabelSort = "name" | "usage_count" | "created_at";

/**
 * Paginated label search. `cursor` is opaque and belongs to the `sort` that produced it —
 * the server rejects one issued under a different ordering rather than silently returning
 * a page from the wrong position, so a sort change has to restart from no cursor.
 */
export function searchLabels(
  token: string,
  options: { q?: string; sort?: LabelSort; cursor?: string; limit?: number } = {},
): Promise<LabelSearchPage> {
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.sort) query.set("sort", options.sort);
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.limit) query.set("limit", String(options.limit));
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

export interface Folder {
  label_id: string | null;
  name: string;
  is_default: boolean;
  documents: number;
  ready: number;
  processing: number;
  failed: number;
}

export interface FolderTree {
  folders: Folder[];
  total_documents: number;
}

/**
 * The folder tree, grouped by the server.
 *
 * The client is a presentation layer here on purpose: a browser rebuilding this from the
 * flat listing would have to re-implement the rule that an unlabelled document is visible
 * tenant-wide while a labelled one is not — which lives in an RLS policy, and getting it
 * wrong means showing a folder to somebody who cannot open anything inside it.
 */
export function folders(token: string): Promise<FolderTree> {
  return request<FolderTree>("/documents/folders", token);
}

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

export interface Role {
  id: string;
  name: string;
  is_system: boolean;
  permissions: string[];
  users: number;
}

export function roles(token: string): Promise<Role[]> {
  return request<Role[]>("/roles", token);
}

export function setRolePermissions(
  token: string,
  roleId: string,
  permissions: string[],
): Promise<Role> {
  return request<Role>(`/roles/${roleId}/permissions`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ permissions }),
  });
}

export interface LlmConfig {
  endpoint_url: string;
  model_name: string;
  /** Never the key itself — only whether one is stored. */
  has_api_key: boolean;
  configured: boolean;
}

export function llmConfig(token: string): Promise<LlmConfig> {
  return request<LlmConfig>("/llm-config", token);
}

export function saveLlmConfig(
  token: string,
  config: { endpoint_url: string; model_name: string; api_key?: string },
): Promise<LlmConfig> {
  // `api_key` omitted rather than sent empty when the field was left alone: the server
  // reads omission as "keep the stored key" and "" as "clear it", and an administrator
  // editing a model name must not silently wipe a credential they cannot even read.
  return request<LlmConfig>("/llm-config", token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}


export interface Invitation {
  user_id: string;
  email: string;
  /**
   * Shown exactly once and not recoverable.
   *
   * There is no mail server on an on-premise install, so the administrator passes this on
   * however they already pass on credentials. The UI must therefore make it obvious that
   * closing the dialog loses it.
   */
  password: string;
  role_ids: string[];
}

export function inviteUser(
  token: string,
  email: string,
  roleIds: string[],
): Promise<Invitation> {
  return request<Invitation>("/users/invite", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role_ids: roleIds }),
  });
}
