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
  return (await response.json()) as T;
}

export function login(email: string, password: string): Promise<{ access_token: string }> {
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
    return response.json() as Promise<{ access_token: string }>;
  });
}

export function tenantStatus(token: string): Promise<TenantStatus> {
  return request<TenantStatus>("/tenant/status", token);
}

export interface UploadResult {
  id: string;
  filename: string;
  status: string;
}

export function uploadDocument(
  file: File,
  token: string,
  labels?: string[],
): Promise<UploadResult> {
  const body = new FormData();
  body.append("file", file);
  for (const label of labels ?? []) body.append("labels", label);
  // No `Content-Type` header: the browser must set it, because only the browser knows the
  // multipart boundary it generated. Setting it by hand produces a body the server cannot
  // parse, and the error says nothing about why.
  return request<UploadResult>("/documents", token, { method: "POST", body });
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
