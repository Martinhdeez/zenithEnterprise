/**
 * Tenant administration: roles, their permissions, the generation connector, invitations.
 */

import { request } from "@/shared/api/http";

export interface Role {
  id: string;
  name: string;
  is_system: boolean;
  permissions: string[];
  users: number;
  /** Clearance, 1–10: the highest label level this role's holders reach through a group. */
  priority_level: number;
  /** Labels granted outright, ignoring both group and clearance. */
  label_ids: string[];
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

export function createRole(
  token: string,
  role: { name: string; permissions: string[]; priority_level: number },
): Promise<Role> {
  return request<Role>("/roles", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role),
  });
}

export function setRoleClearance(token: string, roleId: string, level: number): Promise<Role> {
  return request<Role>(`/roles/${roleId}/clearance`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priority_level: level }),
  });
}

export function deleteRole(token: string, roleId: string): Promise<void> {
  return request<void>(`/roles/${roleId}`, token, { method: "DELETE" });
}

/**
 * A functional group: the horizontal axis.
 *
 * `label_ids` is what the group opens *to a member who also carries the clearance each
 * label demands* — not a list of what any particular member can read. The two halves are
 * independent, and a screen that presents this as "these members see these labels" would
 * be describing an access model the server does not implement.
 */
export interface Group {
  id: string;
  name: string;
  description: string | null;
  members: number;
  label_ids: string[];
}

export function groups(token: string): Promise<Group[]> {
  return request<Group[]>("/groups", token);
}

export function createGroup(
  token: string,
  group: { name: string; description?: string | null },
): Promise<Group> {
  return request<Group>("/groups", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(group),
  });
}

export function deleteGroup(token: string, groupId: string): Promise<void> {
  return request<void>(`/groups/${groupId}`, token, { method: "DELETE" });
}

/** Replace, not patch — the caller sends what the group should open afterwards. */
export function setGroupLabels(
  token: string,
  groupId: string,
  labelIds: string[],
): Promise<Group> {
  return request<Group>(`/groups/${groupId}/labels`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label_ids: labelIds }),
  });
}

export function setGroupMembers(
  token: string,
  groupId: string,
  userIds: string[],
): Promise<Group> {
  return request<Group>(`/groups/${groupId}/members`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_ids: userIds }),
  });
}

/** Classify a label, or declassify it back to 0. */
export function setLabelClearance(
  token: string,
  labelId: string,
  level: number,
): Promise<{ id: string; name: string; priority_level: number }> {
  return request(`/labels/${labelId}/clearance`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priority_level: level }),
  });
}

/** Somebody in this tenant, with the access they hold. */
export interface Member {
  id: string;
  email: string;
  /** Null for anyone who never set one — render the address instead. */
  name: string | null;
  role_ids: string[];
  group_ids: string[];
}

export function users(token: string): Promise<Member[]> {
  return request<Member[]>("/users", token);
}

/**
 * Replace one person's groups.
 *
 * The whole set, not a delta — the same contract every other access write in this API
 * uses, so a caller always knows what the result will be without reading state first.
 */
export function setUserGroups(token: string, userId: string, groupIds: string[]): Promise<void> {
  return request<void>(`/users/${userId}/groups`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group_ids: groupIds }),
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
   * Shown exactly once and not recoverable — and a link now, not a password.
   *
   * There is still no mail server on an on-premise install, so the administrator passes
   * this on however they already share things. What changed is what they are sharing: a
   * password works forever and is the person's real credential, while this is spent on
   * first use and expires on its own.
   *
   * A path, not a full URL. The server does not reliably know the hostname it is reached
   * by — behind a proxy it sees its own container name — so the browser, which does know,
   * puts the origin on the front.
   */
  path: string;
  expires_at: string;
  role_ids: string[];
}

/** The link, as something a person can paste. Origin from the browser; path from the API. */
export function absoluteLink(path: string): string {
  return `${window.location.origin}${path}`;
}

/**
 * A single-use link so somebody can set a new password.
 *
 * This replaces `zenith reset-password` over SSH, which did not survive a third customer:
 * every forgotten password was an escalation to whoever held the server key.
 */
export function issueResetLink(token: string, userId: string): Promise<Invitation> {
  return request<Invitation>(`/users/${userId}/reset-link`, token, { method: "POST" });
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

/** What the installation has been asked, and what the answers read. */
export interface Analytics {
  window_days: number;
  totals: {
    queries: number;
    users: number;
    prompt_tokens: number;
    completion_tokens: number;
    /** Queries whose provider reported no usage. Not the same as costing nothing. */
    queries_without_usage: number;
    average_retrieval_ms: number;
    average_generation_ms: number;
    abstentions: number;
  };
  most_active: { user_id: string | null; email: string | null; queries: number }[];
  top_cited: { document_id: string; filename: string; answers: number }[];
}

export interface AuditEntry {
  query_id: string;
  asked_at: string;
  email: string | null;
  question: string;
  abstained: boolean;
  model: string | null;
  latency_ms: number;
  /** The documents this answer read. */
  documents: string[];
}

export interface AuditPage {
  entries: AuditEntry[];
  /** Opaque. Pass it back as `cursor`; null means this is the last page. */
  next_cursor: string | null;
}

/**
 * One page of the audit log, newest first.
 *
 * Its own request rather than a field on `analytics()`: a page turn cannot change a single
 * aggregate up there, and re-running four of them to fetch ten log rows is work nobody
 * asked for.
 */
export function auditLog(token: string, cursor?: string | null): Promise<AuditPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return request<AuditPage>(`/analytics/audit${query}`, token);
}

/**
 * One change to who can see what.
 *
 * Deliberately not merged with `AuditEntry` above, which is a question somebody *asked*.
 * The two lived under the single word "audit" for a while, and that is precisely how a
 * product comes to believe it has an audit trail because a screen is called one.
 */
export interface AuditEvent {
  id: string;
  /** Copied at write time, so a departed administrator's actions still name them. */
  actor_email: string;
  /** A stable `subject.verb` token. `describe()` in AuditTrail.tsx turns it into English —
      the API never sends prose, because prose cannot be filtered or counted. */
  action: string;
  target_type: string | null;
  target_id: string | null;
  /** What the target was called at the time, since it may have been renamed since. */
  target_name: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface AuditEventPage {
  events: AuditEvent[];
  next_cursor: string | null;
}

export function auditEvents(token: string, cursor?: string | null): Promise<AuditEventPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return request<AuditEventPage>(`/analytics/audit-events${query}`, token);
}

export function analytics(token: string): Promise<Analytics> {
  return request<Analytics>("/analytics", token);
}
