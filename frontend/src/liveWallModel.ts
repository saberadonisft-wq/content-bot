import type {
  ChannelSubscription,
  LiveWallSlots,
  Source,
} from "./api";

export function initialLiveWallChannelIds(
  channels: ChannelSubscription[],
  configuredIds: string[],
) {
  const availableIds = new Set(
    channels
      .map((channel) => channel.id)
      .filter((id): id is string => Boolean(id)),
  );
  const configured = configuredIds.filter(
    (id, index) => availableIds.has(id) && configuredIds.indexOf(id) === index,
  );
  if (configured.length) return configured;

  return [...availableIds];
}

export function xProfileUsername(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (!/(^|\.)(?:x|twitter)\.com$/i.test(parsed.hostname)) return null;
    const match = parsed.pathname.match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
    if (!match) return null;
    const reservedPaths = new Set([
      "explore",
      "hashtag",
      "home",
      "i",
      "intent",
      "messages",
      "notifications",
      "search",
      "settings",
      "share",
    ]);
    return reservedPaths.has(match[1].toLowerCase()) ? null : match[1];
  } catch {
    return null;
  }
}

export function tiktokCreatorUsername(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (!/(^|\.)tiktok\.com$/i.test(parsed.hostname)) return null;
    const match = parsed.pathname.match(/^\/@([A-Za-z0-9._-]{2,64})\/?$/);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

export function channelSupportsEmbed(
  channel: ChannelSubscription,
  sources: Source[],
) {
  return sources.some(
    (source) =>
      source.id === channel.source_id &&
      source.operations.some(
        (operation) => operation.id === "render_embed" && operation.enabled,
      ),
  );
}

export function liveWallPage<T>(channels: T[], slots: LiveWallSlots, page: number) {
  const pageCount = Math.max(1, Math.ceil(channels.length / slots));
  const safePage = Math.max(0, Math.min(page, pageCount - 1));
  return {
    pageCount,
    safePage,
    channels: channels.slice(safePage * slots, safePage * slots + slots),
  };
}
