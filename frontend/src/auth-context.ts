import { createContext } from "react";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "user";
  status: "pending" | "approved" | "rejected" | "banned";
  auth_provider: "email" | "google";
  avatar_url?: string | null;
  created_at: string;
  approved_at?: string | null;
  approved_by?: string | null;
  last_login_at?: string | null;
}

export interface AuthContextType {
  user: AuthUser | null;
  accessToken: string | null;
  isLoading: boolean;
  error: string | null;
  isAuthenticated: boolean;
  isApproved: boolean;
  isPending: boolean;
  isBanned: boolean;
  isAdmin: boolean;
  login: (email: string, pass: string) => Promise<void>;
  register: (email: string, pass: string, name: string) => Promise<string>;
  logout: () => Promise<void>;
  checkStatus: () => Promise<AuthUser | null>;
  startGoogleLogin: () => void;
  authServerUrl: string;
}

export const AUTH_SERVER_URL =
  import.meta.env.VITE_AUTH_SERVER_URL ?? "http://127.0.0.1:8080";

export const STORAGE_ACCESS_KEY = "content_bot_access_token";
export const STORAGE_REFRESH_KEY = "content_bot_refresh_token";
export const STORAGE_USER_KEY = "content_bot_user";

export const AuthContext = createContext<AuthContextType | undefined>(undefined);
