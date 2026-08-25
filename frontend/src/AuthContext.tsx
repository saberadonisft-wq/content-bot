import React, {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  AUTH_SERVER_URL,
  AuthContext,
  STORAGE_ACCESS_KEY,
  STORAGE_REFRESH_KEY,
  STORAGE_USER_KEY,
  type AuthUser,
} from "./auth-context";

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

  const persistAuth = useCallback((
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
  }, []);

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
  }, [accessToken, refreshToken, persistAuth]);

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
    return null;
  }, [accessToken, refreshToken, logout, persistAuth]);

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
  }, [accessToken, checkStatus, persistAuth]);

  useEffect(() => {
    const desktop = window.contentBotDesktop;
    if (!desktop) return undefined;
    return desktop.onGoogleAuthResult((result) => {
      if (result.error) {
        setError(result.error);
        setIsLoading(false);
        return;
      }
      if (!result.accessToken || !result.refreshToken) {
        setError("Desktop không nhận được phiên đăng nhập Google hợp lệ.");
        setIsLoading(false);
        return;
      }
      setIsLoading(true);
      void fetch(`${AUTH_SERVER_URL}/auth/me`, {
        headers: { Authorization: `Bearer ${result.accessToken}` },
      })
        .then(async (response) => {
          if (!response.ok) throw new Error("Không xác minh được tài khoản Google.");
          const profile: AuthUser = await response.json();
          persistAuth(result.accessToken as string, result.refreshToken as string, profile);
          setError(null);
        })
        .catch((reason: unknown) => {
          setError(reason instanceof Error ? reason.message : "Đăng nhập Google thất bại.");
        })
        .finally(() => setIsLoading(false));
    });
  }, [persistAuth]);

  const login = useCallback(async (email: string, pass: string) => {
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
  }, [persistAuth]);

  const register = useCallback(async (
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
  }, []);

  const startGoogleLogin = useCallback(() => {
    if (window.contentBotDesktop) {
      setError(null);
      void window.contentBotDesktop.startGoogleLogin().catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "Không mở được đăng nhập Google trên trình duyệt.",
        );
      });
      return;
    }
    window.location.href = `${AUTH_SERVER_URL}/auth/google`;
  }, []);

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
      login,
      register,
      logout,
      checkStatus,
      startGoogleLogin,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
