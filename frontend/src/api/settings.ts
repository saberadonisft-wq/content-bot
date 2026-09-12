import { request } from "../transport/client";
import type { CredentialStatus, UpdateCheckResponse } from "./types";
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