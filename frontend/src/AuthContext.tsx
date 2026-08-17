import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

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

const STORAGE_ACCESS_KEY = "content_bot_access_token";
const STORAGE_REFRESH_KEY = "content_bot_refresh_token";
const STORAGE_USER_KEY = "content_bot_user";

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [user, setUser] = useState<AuthUser | null>(() => {
    try {
      const cached = localStorage.getItem(STORAGE_USER_KEY);
      return cached ? JSON.parse(cached) : null;
    } catch {
      return null;
    }
  });
  const [accessToken, setAccessToken] = useState<string | null>(() =>
    localStorage.getItem(STORAGE_ACCESS_KEY),
  );
  const [refreshToken, setRefreshToken] = useState<string | null>(() =>
    localStorage.getItem(STORAGE_REFRESH_KEY),
  );
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const persistAuth = (
    token: string | null,
    refresh: string | null,
    u: AuthUser | null,
  ) => {
    setAccessToken(token);
    setRefreshToken(refresh);
    setUser(u);
    if (token) {
      localStorage.setItem(STORAGE_ACCESS_KEY, token);
    } else {
      localStorage.removeItem(STORAGE_ACCESS_KEY);
    }
    if (refresh) {
      localStorage.setItem(STORAGE_REFRESH_KEY, refresh);
    } else {
      localStorage.removeItem(STORAGE_REFRESH_KEY);
    }
    if (u) {
      localStorage.setItem(STORAGE_USER_KEY, JSON.stringify(u));
    } else {
      localStorage.removeItem(STORAGE_USER_KEY);
    }
  };

  const logout = useCallback(async () => {
    if (refreshToken && accessToken) {
      try {
        await fetch(`${AUTH_SERVER_URL}/auth/logout`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${accessToken}`,
          },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      } catch {
        // ignore logout network errors
      }
    }
    persistAuth(null, null, null);
    setError(null);
  }, [accessToken, refreshToken]);

  const checkStatus = useCallback(async (): Promise<AuthUser | null> => {
    if (!accessToken) return null;
    try {
      const resp = await fetch(`${AUTH_SERVER_URL}/auth/me`, {
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      });
      if (resp.ok) {
        const profile: AuthUser = await resp.json();
        setUser(profile);
        localStorage.setItem(STORAGE_USER_KEY, JSON.stringify(profile));
        return profile;
      } else if (resp.status === 401 && refreshToken) {
        // Try refresh token
        const refResp = await fetch(`${AUTH_SERVER_URL}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (refResp.ok) {
          const data = await refResp.json();
          persistAuth(data.access_token, data.refresh_token, data.user);
          return data.user;
        } else {
          await logout();
          return null;
        }
      }
    } catch {
      // offline or server down
    }
    return user;
  }, [accessToken, refreshToken, user, logout]);

  // Initial URL Google OAuth parse & token verification
  useEffect(() => {
    const initAuth = async () => {
      const url = new URL(window.location.href);
      const googleToken = url.searchParams.get("auth_token");
      const googleRefresh = url.searchParams.get("refresh_token");
      const authError = url.searchParams.get("auth_error");

      if (authError) {
        setError(decodeURIComponent(authError));
        // clean url
        url.searchParams.delete("auth_error");
        window.history.replaceState({}, "", url.pathname + url.search);
        setIsLoading(false);
        return;
      }

      if (googleToken && googleRefresh) {
        // Google login success callback
        url.searchParams.delete("auth_token");
        url.searchParams.delete("refresh_token");
        url.searchParams.delete("user_status");
        url.searchParams.delete("user_role");
        url.searchParams.delete("user_email");
        window.history.replaceState({}, "", url.pathname + url.search);

        try {
          const resp = await fetch(`${AUTH_SERVER_URL}/auth/me`, {
            headers: { Authorization: `Bearer ${googleToken}` },
          });
          if (resp.ok) {
            const u = await resp.json();
            persistAuth(googleToken, googleRefresh, u);
          }
        } catch {
          // fallback
          persistAuth(googleToken, googleRefresh, null);
        }
        setIsLoading(false);
        return;
      }

      // Existing stored token verification
      if (accessToken) {
        await checkStatus();
      }
      setIsLoading(false);
    };

    initAuth();
  }, []);

  const login = async (email: string, pass: string) => {
    setError(null);
    setIsLoading(true);
    try {
      const res = await fetch(`${AUTH_SERVER_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password: pass }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Đăng nhập không thành công.");
      }
      persistAuth(data.access_token, data.refresh_token, data.user);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Đăng nhập thất bại.";
      setError(msg);
      throw err;
    } finally {
      setIsLoading(false);
    }
  };

  const register = async (
    email: string,
    pass: string,
    name: string,
  ): Promise<string> => {
    setError(null);
    setIsLoading(true);
    try {
      const res = await fetch(`${AUTH_SERVER_URL}/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          password: pass,
          display_name: name,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Đăng ký không thành công.");
      }
      return data.message || "Đăng ký thành công!";
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Đăng ký thất bại.";
      setError(msg);
      throw err;
    } finally {
      setIsLoading(false);
    }
  };

  const startGoogleLogin = () => {
    window.location.href = `${AUTH_SERVER_URL}/auth/google`;
  };

  const isAuthenticated = Boolean(accessToken && user);
  const isApproved = user?.status === "approved";
  const isPending = user?.status === "pending";
  const isBanned = user?.status === "banned";
  const isAdmin = user?.role === "admin";

  const value = useMemo(
    () => ({
      user,
      accessToken,
      isLoading,
      error,
      isAuthenticated,
      isApproved,
      isPending,
      isBanned,
      isAdmin,
      login,
      register,
      logout,
      checkStatus,
      startGoogleLogin,
      authServerUrl: AUTH_SERVER_URL,
    }),
    [
      user,
      accessToken,
      isLoading,
      error,
      isAuthenticated,
      isApproved,
      isPending,
      isBanned,
      isAdmin,
      logout,
      checkStatus,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
};
