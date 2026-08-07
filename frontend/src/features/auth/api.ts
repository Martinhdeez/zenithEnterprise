/**
 * Login, the silent refresh behind it, and who you are once you are in.
 */

import { ApiError, request, type ApiErrorBody } from "@/shared/api/http";

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


export interface UserProfile {
  user_id: string;
  email: string;
  name: string | null;
  tenant_id: string;
  tenant_name: string | null;
  roles: string[];
  permissions: string[];
  /**
   * The labels this caller reaches, by name. The reason the profile screen is worth
   * opening: it is the answer to "why can I not see the document my colleague can", which
   * is always that the label behind it is not one of these.
   */
  labels: string[];
  documents_uploaded: number;
  created_at: string;
}

export function profile(token: string): Promise<UserProfile> {
  return request<UserProfile>("/auth/profile", token);
}

/** Signs every other session out as a side effect — see the endpoint's own docstring. */
export function changePassword(
  token: string,
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return request<void>("/auth/password", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

export function signOutEverywhere(token: string): Promise<void> {
  return request<void>("/auth/sign-out-everywhere", token, { method: "POST" });
}

/** The only field on the profile its owner may set. Returns the whole profile back. */
export function renameSelf(token: string, name: string): Promise<UserProfile> {
  return request<UserProfile>("/auth/profile", token, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}
