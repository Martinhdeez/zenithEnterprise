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
