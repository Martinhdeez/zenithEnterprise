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
