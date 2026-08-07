/**
 * Login and the silent refresh behind it.
 */

import { ApiError, type ApiErrorBody } from "@/shared/api/http";

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
