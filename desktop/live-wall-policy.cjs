const SOURCE_HOSTS = Object.freeze({
  bilibili: ["bilibili.com", "b23.tv"],
  bluesky: ["bsky.app"],
  douyin: ["douyin.com"],
  facebook: ["facebook.com", "fb.com", "fb.watch"],
  instagram: ["instagram.com"],
  kuaishou: ["kuaishou.com"],
  reddit: ["reddit.com", "redd.it"],
  steam: ["store.steampowered.com", "steamcommunity.com"],
  tieba: ["tieba.baidu.com"],
  tiktok: ["tiktok.com"],
  weibo: ["weibo.com", "weibo.cn"],
  x: ["x.com", "twitter.com"],
  xhs: ["xiaohongshu.com", "rednote.com", "xhslink.com"],
  youtube: ["youtube.com", "youtu.be"],
  zhihu: ["zhihu.com"],
});

const SOURCE_AUTH_HOSTS = Object.freeze({
  x: ["accounts.google.com", "accounts.googleusercontent.com"],
});

function hostnameMatches(hostname, allowed) {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  return allowed.some(
    (domain) => normalized === domain || normalized.endsWith(`.${domain}`),
  );
}

function safeChannelUrl(sourceId, value) {
  const allowed = SOURCE_HOSTS[sourceId];
  if (!allowed || typeof value !== "string" || value.length > 2048) return null;
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      !hostnameMatches(parsed.hostname, allowed)
    ) {
      return null;
    }
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return null;
  }
}

function safeAuthUrl(sourceId, value) {
  const sourceHosts = SOURCE_HOSTS[sourceId] || [];
  const authHosts = SOURCE_AUTH_HOSTS[sourceId] || [];
  if (!authHosts.length || typeof value !== "string" || value.length > 4096) return null;
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      !hostnameMatches(parsed.hostname, [...sourceHosts, ...authHosts])
    ) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

function safeSubtitleDownload(value, filename) {
  if (typeof value !== "string" || value.length > 4096) return null;
  const safeFilename = String(filename || "");
  if (!/^subtitled_[a-f0-9]{12,32}_[a-f0-9]{12}\.mp4$/.test(safeFilename)) {
    return null;
  }
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      parsed.port !== "8000" ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== `/api/v1/subtitles/renders/${safeFilename}`
    ) {
      return null;
    }
    for (const key of parsed.searchParams.keys()) {
      if (key !== "t") return null;
    }
    return { url: parsed.toString(), filename: safeFilename };
  } catch {
    return null;
  }
}

function normalizeBounds(value) {
  if (!value || typeof value !== "object") return null;
  const bounds = {
    x: Math.round(Number(value.x)),
    y: Math.round(Number(value.y)),
    width: Math.round(Number(value.width)),
    height: Math.round(Number(value.height)),
  };
  if (
    !Object.values(bounds).every(Number.isFinite) ||
    bounds.x < -20_000 ||
    bounds.y < -12_000 ||
    bounds.width < 80 ||
    bounds.height < 80 ||
    bounds.x > 20_000 ||
    bounds.y > 20_000 ||
    bounds.width > 20_000 ||
    bounds.height > 12_000
  ) {
    return null;
  }
  return bounds;
}

function clippedViewBounds(bounds, viewport) {
  const left = Math.max(0, bounds.x);
  const top = Math.max(0, bounds.y);
  const right = Math.min(viewport.width, bounds.x + bounds.width);
  const bottom = Math.min(viewport.height, bounds.y + bounds.height);
  const width = Math.floor(right - left);
  const height = Math.floor(bottom - top);
  if (width < 1 || height < 1) return null;
  return {
    container: { x: left, y: top, width, height },
    content: {
      x: bounds.x - left,
      y: bounds.y - top,
      width: bounds.width,
      height: bounds.height,
    },
  };
}

function normalizeSyncPayload(payload) {
  if (!payload || typeof payload !== "object") {
    throw new TypeError("Desktop Live Wall payload is required.");
  }
  const ownerId = String(payload.ownerId || "").trim();
  const keywordId = Number(payload.keywordId);
  if (!ownerId || ownerId.length > 512) {
    throw new TypeError("Desktop Live Wall owner is invalid.");
  }
  if (!Number.isInteger(keywordId) || keywordId <= 0) {
    throw new TypeError("Desktop Live Wall keyword is invalid.");
  }
  if (!Array.isArray(payload.views) || payload.views.length > 6) {
    throw new TypeError("Desktop Live Wall accepts at most six views.");
  }
  const seen = new Set();
  const views = payload.views.map((candidate) => {
    const channelId = String(candidate?.channelId || "").trim();
    const sourceId = String(candidate?.sourceId || "").trim().toLowerCase();
    const url = safeChannelUrl(sourceId, candidate?.url);
    const bounds = normalizeBounds(candidate?.bounds);
    if (
      !/^[a-zA-Z0-9_-]{1,128}$/.test(channelId) ||
      seen.has(channelId) ||
      !url ||
      !bounds
    ) {
      throw new TypeError("Desktop Live Wall contains an invalid channel view.");
    }
    seen.add(channelId);
    return {
      channelId,
      sourceId,
      url,
      bounds,
      visible: Boolean(candidate.visible),
    };
  });
  return { ownerId, keywordId, views };
}

module.exports = {
  SOURCE_AUTH_HOSTS,
  SOURCE_HOSTS,
  clippedViewBounds,
  normalizeBounds,
  normalizeSyncPayload,
  safeAuthUrl,
  safeChannelUrl,
  safeSubtitleDownload,
};
