/**
 * The one place a request to this API is made, and the one shape an error comes back in.
 *
 * Relative paths throughout. The bundle is served next to the API inside the customer's
 * network, so a baked-in host would be a value that is wrong on every installation except
 * the one it was built for — and an on-premise product is only ever installed somewhere
 * else.
 *
 * Shared rather than per-feature: every feature's `api.ts` calls `request`, and a second
 * copy of this would be a second place for the bearer header, the 204 special case and the
 * RFC 7807 unwrapping to drift.
 */

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

export async function request<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
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
