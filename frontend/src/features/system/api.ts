/**
 * Organisations, from above every tenant.
 *
 * The only client in the app that talks about more than one customer. Every route here is
 * refused server-side without `users.is_system_admin`, which is not a permission a tenant
 * can grant itself — see `requires_system_admin` on the backend.
 */

import { request } from "@/shared/api/http";

export type OrganisationStatus = "active" | "suspended" | "purging" | "purged";

export interface Organisation {
  id: string;
  name: string;
  status: OrganisationStatus;
  created_at: string;
  status_changed_at: string | null;
  users: number;
  documents: number;
  /** What the rows claim, not what the disk holds. */
  storage_bytes: number;
}

export function organisations(token: string): Promise<Organisation[]> {
  return request<Organisation[]>("/system/tenants", token);
}

export interface Provisioned {
  organisation: Organisation;
  admin_email: string;
  /** Shown once, never recoverable. Same contract as inviting a colleague. */
  password: string;
}

export function provisionOrganisation(
  token: string,
  body: { name: string; admin_email: string },
): Promise<Provisioned> {
  return request<Provisioned>("/system/tenants", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function suspendOrganisation(token: string, id: string): Promise<Organisation> {
  return request<Organisation>(`/system/tenants/${id}/suspend`, token, { method: "POST" });
}

export function activateOrganisation(token: string, id: string): Promise<Organisation> {
  return request<Organisation>(`/system/tenants/${id}/activate`, token, { method: "POST" });
}

/**
 * Destroy an organisation's data.
 *
 * `confirmName` is not decoration and is not checked only here: the server compares it too,
 * and refuses outright unless the organisation is already suspended. A wrong build of this
 * file cannot destroy a customer on its own.
 */
export function purgeOrganisation(
  token: string,
  id: string,
  confirmName: string,
): Promise<Organisation> {
  return request<Organisation>(`/system/tenants/${id}/purge`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm_name: confirmName }),
  });
}
