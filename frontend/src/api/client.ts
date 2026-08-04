/**
 * Everything that is not the stream: login, upload, status.
 *
 * Relative paths throughout. The bundle is served next to the API inside the customer's
 * network, so a baked-in host would be a value that is wrong on every installation except
 * the one it was built for — and an on-premise product is only ever installed somewhere
 * else.
 */

export interface TenantStatus {
  documents: Record<string, number>;
  chunks: number;
  hardware: string;
  components: { embeddings: boolean; reranker: boolean; generation: boolean };
  searchable: boolean;
}

export interface ApiErrorBody {
  code: string;
  message: string;
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
    throw new ApiError(response.status, body.code, body.message);
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
      throw new ApiError(response.status, body.code, body.message);
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
