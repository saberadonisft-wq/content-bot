import { useState } from "react";

export function useRenderedVideoDownload(url: string | null, filename: string | undefined,
  onError: (message: string | null) => void) {
  const [downloading, setDownloading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const download = async () => {
    const bridge = window.contentBotDesktop;
    if (!url || !filename || !bridge) return;
    setDownloading(true);
    setMessage("Đang chờ chọn nơi lưu…");
    onError(null);
    try {
      const result = await bridge.downloadFile({url, filename,
        accessToken: localStorage.getItem("content_bot_access_token") ?? undefined});
      setMessage(result.state === "completed" ? "Đã tải video" : result.state === "cancelled" ? null : "Tải video bị gián đoạn");
      if (result.state === "interrupted") onError("Tải video bị gián đoạn. Kiểm tra backend và thử lại.");
    } catch (error) {
      setMessage(null);
      onError(error instanceof Error ? error.message : "Không tải được video đã xuất.");
    } finally {
      setDownloading(false);
    }
  };
  return { downloading, message, download };
}
