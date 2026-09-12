export const API_BASE =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

export function getAuthHeader(): Record<string, string> {
  const token = localStorage.getItem("content_bot_access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeader(),
        ...init?.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new Error(
      `Không thể kết nối backend tại ${API_BASE}. Hãy khởi động backend rồi thử lại.`,
      { cause: error },
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : `Request failed (${response.status})`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}