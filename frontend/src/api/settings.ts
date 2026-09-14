import { request } from "../transport/client";
import type { CredentialStatus, UpdateCheckResponse } from "./types";
export type GeminiKey = {
  id: string; name: string; enabled: boolean; project_group: string | null;
  masked_key: string; state: string; checked_model: string | null; checked_at: string | null;
};
export type GeminiKeyList = { version: number; configured: boolean; keys: GeminiKey[]; added?: number; duplicates?: number };
export const getGeminiKeys = (signal?: AbortSignal) => request<GeminiKeyList>("/credentials/gemini/keys", { signal });
export const importGeminiKeys = (keys: string) => request<GeminiKeyList>("/credentials/gemini/keys", { method: "POST", body: JSON.stringify({ keys }) });
export const updateGeminiKey = (id: string, changes: Partial<Pick<GeminiKey, "name" | "enabled" | "project_group">>) => request<GeminiKeyList>(`/credentials/gemini/keys/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
export const deleteGeminiKey = (id: string) => request<GeminiKeyList>(`/credentials/gemini/keys/${id}`, { method: "DELETE" });
export const checkGeminiKey = (id: string, model: string) => request<GeminiKeyList>(`/credentials/gemini/keys/${id}/check`, { method: "POST", body: JSON.stringify({ model }) });
export const getCredentialStatus = (signal?: AbortSignal) =>
  request<CredentialStatus>("/credentials/status", { signal });

export const setupMasterPassword = (
  masterPassword: string,
  initialCredentials?: Record<string, string>,
) =>
  request<{ success: boolean; message: string; status: CredentialStatus }>(
    "/credentials/setup",
    {
      method: "POST",
      body: JSON.stringify({
        master_password: masterPassword,
        initial_credentials: initialCredentials,
      }),
    },
  );

export const unlockCredentials = (masterPassword: string) =>
  request<{ success: boolean; message: string; status: CredentialStatus }>(
    "/credentials/unlock",
    {
      method: "POST",
      body: JSON.stringify({ master_password: masterPassword }),
    },
  );

export const lockCredentials = () =>
  request<{ success: boolean; message: string; status: CredentialStatus }>(
    "/credentials/lock",
    {
      method: "POST",
    },
  );

export const changeMasterPassword = (oldPassword: string, newPassword: string) =>
  request<{ success: boolean; message: string; status: CredentialStatus }>(
    "/credentials/change-password",
    {
      method: "POST",
      body: JSON.stringify({
        old_password: oldPassword,
        new_password: newPassword,
      }),
    },
  );

export const updateCredentials = (credentials: Record<string, string>) =>
  request<{ success: boolean; message: string; status: CredentialStatus }>(
    "/credentials",
    {
      method: "PUT",
      body: JSON.stringify({ credentials }),
    },
  );

export const getAppVersion = (signal?: AbortSignal) =>
  request<{ version: string }>("/version", { signal });

export const checkForUpdate = (channel: "stable" | "beta" = "stable", signal?: AbortSignal) =>
  request<UpdateCheckResponse>(`/update/check?channel=${channel}`, { signal });
