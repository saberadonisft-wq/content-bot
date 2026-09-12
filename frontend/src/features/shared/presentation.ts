
import {
  Database,
  Monitor,
  Video,
  Waypoints
} from "lucide-react";
import {
  ChannelSubscription,
  Item,
  Source,
  SourceRun
} from "../../api";

export type LibraryTab = "channels" | "live" | "connections" | "videos";
export const LIBRARY_TABS: { id: LibraryTab; label: string; icon: typeof Database }[] = [
  { id: "channels", label: "Kênh theo dõi", icon: Waypoints },
  { id: "live", label: "Trực tiếp", icon: Monitor },
  { id: "connections", label: "Kết nối nền tảng", icon: Database },
  { id: "videos", label: "Video", icon: Video },
];
export const splitTerms = (value: string) =>
  value
    .split(",")
    .map((term) => term.trim())
    .filter(Boolean);

export const sourceOperation = (source: Source, operationId: string) =>
  source.operations?.find(
    (operation) => operation.id === operationId && operation.enabled,
  ) ?? source.operations?.find((operation) => operation.id === operationId);

export const sourceCanRun = (source: Source, operationId: string) =>
  source.operations?.some(
    (operation) => operation.id === operationId && operation.enabled,
  ) ?? false;

export const channelNeedsPaidAccess = (channel: ChannelSubscription) =>
  channel.source_id === "x" &&
  channel.last_error?.startsWith("PAYMENT_OR_ACCESS_REQUIRED:") === true;

export const channelErrorDetail = (channel: ChannelSubscription) =>
  channelNeedsPaidAccess(channel)
    ? "X API chưa có credits hoặc access tier phù hợp. Kênh vẫn có thể mở để xem; chỉ thử quét lại sau khi đã cấp quyền trong X Developer Console."
    : channel.last_error;

/** Extract a human-readable channel name from the URL when label is empty. */
export const channelDisplayName = (channel: ChannelSubscription): string => {
  if (channel.label) return channel.label;
  const raw = channel.normalized_url || channel.url;
  try {
    const parsed = new URL(raw);
    const parts = parsed.pathname.split("/").filter(Boolean);
    const src = channel.source_id ?? "";
    if (["x", "tiktok", "instagram"].includes(src) && parts.length) {
      return `@${parts[parts.length - 1].replace(/^@/, "")}`;
    }
    if (src === "youtube" && parts.length) {
      return parts[parts.length - 1];
    }
    if (src === "bluesky" && parts.length >= 2 && parts[0] === "profile") {
      return `@${parts[1]}`;
    }
    if (src === "mastodon" && parts.length) {
      const last = parts[parts.length - 1];
      return last.startsWith("@") ? last : `@${last}`;
    }
    if (src === "reddit" && parts.length >= 2) {
      if (parts[0] === "r") return `r/${parts[1]}`;
      if (["u", "user"].includes(parts[0])) return `u/${parts[1]}`;
    }
    if (src === "bilibili" && parts.length) {
      return `Bilibili ${parts[parts.length - 1]}`;
    }
    if (src === "steam" && parts.length >= 2 && parts[0] === "app") {
      return `Steam App ${parts[1]}`;
    }
    if (src === "facebook" && parts.length) return parts[parts.length - 1];
    if (parts.length) return parts[parts.length - 1];
    return parsed.hostname;
  } catch {
    return raw;
  }
};

export const fmt = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat("vi-VN", {
      dateStyle: "short",
      timeStyle: "short",
    }).format(new Date(value))
    : "—";
export const humanMetricLabel = (key: string) => {
  const labels: Record<string, string> = {
    view_count: "Views",
    like_count: "Likes",
    comment_count: "Comments",
    share_count: "Shares",
    favorite_count: "Favorites",
    reaction_count: "Reactions",
  };
  return (
    labels[key] ??
    key
      .replace(/_count$/, "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
};

export const metric = (item: Item, sources: Source[]) => {
  const descriptors =
    sources.find((source) => source.id === item.source_id)?.metrics ?? [];
  const parts: string[] = [];
  const usedKeys = new Set<string>();
  const append = (key: string, label: string) => {
    const value = item.metrics[key];
    if (
      usedKeys.has(key) ||
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      value === 0
    ) {
      return;
    }
    usedKeys.add(key);
    parts.push(`${value.toLocaleString("vi-VN")} ${label}`);
  };

  descriptors.forEach((descriptor) => {
    if (Object.hasOwn(item.metrics, descriptor.id)) {
      append(descriptor.id, descriptor.label);
    }
  });
  descriptors.forEach((descriptor) => {
    const key = descriptor.legacy_key;
    if (!key || usedKeys.has(key) || !Object.hasOwn(item.metrics, key)) return;
    const sharedAlias =
      descriptors.filter((candidate) => candidate.legacy_key === key).length > 1;
    append(key, sharedAlias ? humanMetricLabel(key) : descriptor.label);
  });
  Object.keys(item.metrics).forEach((key) => {
    append(key, humanMetricLabel(key));
  });
  return parts.join(" · ") || "No public metrics";
};

export const phaseLabel = (run: SourceRun) => {
  const labels: Record<string, string> = {
    queued: "Đang chờ",
    checking_source: "Kiểm tra nguồn",
    starting: "Khởi động quét",
    searching: "Đang tìm kiếm",
    opening_browser: "Mở trình duyệt Cốc Cốc",
    waiting_login: "Chờ đăng nhập",
    authenticated: "Đã xác thực",
    scanning: "Đang quét sau đăng nhập",
    completed: "Hoàn thành",
    skipped: "Bỏ qua",
    cancelled: "Đã hủy",
    recovering_browser: "Mở lại Cốc Cốc",
    browser_closed: "Cốc Cốc đã đóng",
    parser_drift: "Cấu trúc nguồn đã thay đổi",
    failed: "Thất bại",
  };
  return labels[run.phase] ?? run.phase.replaceAll("_", " ");
};

export const stateLabel = (state: string) => {
  const labels: Record<string, string> = {
    queued: "Đang chờ",
    running: "Đang chạy",
    succeeded: "Hoàn thành",
    failed: "Thất bại",
    cancelled: "Đã hủy",
  };
  return labels[state] ?? state;
};

export const progressPercent = (run: SourceRun) => {
  if (run.progress_percent !== null) return run.progress_percent;
  if (run.state === "succeeded") return 100;
  if (run.progress_total && run.progress_mode === "determinate") {
    return Math.min((run.progress_current / run.progress_total) * 100, 99);
  }
  return null;
};

export const insightEvidence = (item: Item) =>
  Array.from(
    new Set([
      ...item.insights.sentiment.reasons.map(
        (reason) => reason.split(": ").at(-1) ?? reason,
      ),
      ...item.insights.topics.flatMap((topic) => topic.reasons),
    ]),
  ).slice(0, 6);

export const safeExternalUrl = (value: string) => {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
};

