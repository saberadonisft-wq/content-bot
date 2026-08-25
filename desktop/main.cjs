const crypto = require("node:crypto");
const http = require("node:http");
const path = require("node:path");

const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  shell,
  View,
  WebContentsView,
} = require("electron");

const {
  clippedViewBounds,
  normalizeSyncPayload,
  safeAuthUrl,
  safeChannelUrl,
  safeSubtitleDownload,
} = require("./live-wall-policy.cjs");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const DEV_URL = process.env.CONTENT_BOT_DESKTOP_DEV_URL || "http://127.0.0.1:5173";
const STATUS_CHANNEL = "content-bot:desktop-live-wall-status";
const GOOGLE_AUTH_CHANNEL = "content-bot:desktop-google-auth-result";
const X_LOGIN_URL = "https://x.com/i/flow/login";
const AUTH_SERVER_URL = process.env.CONTENT_BOT_AUTH_SERVER_URL || "http://127.0.0.1:8080";
const MAX_CONCURRENT_LOADS = 1;
const viewRecords = new Map();
const configuredPartitions = new Set();
const loadQueue = [];
let activeLoadCount = 0;
let mainWindow = null;
let activeOwnerKey = null;
let pendingGoogleLogin = null;
let externalAuthNoticeOpen = false;

app.setName("Content Bot Desktop Dev");
app.setPath("userData", path.join(PROJECT_ROOT, "data", "desktop-dev-profile"));
app.commandLine.appendSwitch("autoplay-policy", "document-user-activation-required");
const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

function ownerKey(ownerId) {
  return crypto.createHash("sha256").update(ownerId).digest("hex").slice(0, 24);
}

function sendStatus(record, state, detail) {
  record.state = state;
  record.detail = detail;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(STATUS_CHANNEL, {
      channelId: record.channelId,
      state,
      detail,
    });
  }
}

function statusOf(record) {
  return {
    channelId: record.channelId,
    state: record.state,
    detail: record.detail,
  };
}

function applyVisibility(record) {
  const shouldShow = record.desiredVisible && record.state === "ready";
  record.container.setVisible(shouldShow);
  record.view.setVisible(shouldShow);
}

function releaseLoadingSlot(record) {
  if (!record.loadingSlot) return;
  record.loadingSlot = false;
  activeLoadCount = Math.max(0, activeLoadCount - 1);
}

function clearLoadTimeout(record) {
  if (!record.loadTimeout) return;
  clearTimeout(record.loadTimeout);
  record.loadTimeout = null;
}

function settleRecord(record, state, detail) {
  clearLoadTimeout(record);
  releaseLoadingSlot(record);
  sendStatus(record, state, detail);
  applyVisibility(record);
  pumpLoadQueue();
}

function startQueuedLoad(record) {
  if (record.destroyed || record.loadingSlot) return;
  record.queued = false;
  record.loadingSlot = true;
  activeLoadCount += 1;
  sendStatus(record, "loading", "Đang tải kênh (từng kênh một để giảm tải máy)…");
  applyVisibility(record);
  record.loadTimeout = setTimeout(() => {
    if (!record.destroyed && record.state !== "ready") {
      settleRecord(record, "error", "Kênh tải quá lâu. Bấm “Thử lại” để tải lại riêng kênh này.");
    }
  }, 25_000);
  const operation = record.hasLoaded
    ? record.view.webContents.reload()
    : record.view.webContents.loadURL(record.url);
  record.hasLoaded = true;
  Promise.resolve(operation).catch((error) => {
    if (!record.destroyed) {
      settleRecord(record, "error", `Không tải được kênh (${error.message}).`);
    }
  });
}

function pumpLoadQueue() {
  while (activeLoadCount < MAX_CONCURRENT_LOADS && loadQueue.length) {
    const record = loadQueue.shift();
    if (!record || record.destroyed || !record.queued) continue;
    startQueuedLoad(record);
  }
}

function enqueueLoad(record) {
  if (record.destroyed || record.queued || record.loadingSlot) return;
  record.queued = true;
  sendStatus(record, "queued", "Đang chờ tải để tránh mở nhiều trang nặng cùng lúc…");
  applyVisibility(record);
  loadQueue.push(record);
  pumpLoadQueue();
}

function configurePartition(record) {
  const browserSession = record.view.webContents.session;
  if (configuredPartitions.has(record.partition)) return;
  configuredPartitions.add(record.partition);
  browserSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  browserSession.webRequest.onBeforeRequest((details, callback) => {
    let isHeavyMedia = details.resourceType === "media";
    if (!isHeavyMedia) {
      try {
        isHeavyMedia = /\.(?:m3u8|m4a|mp3|mp4|webm)(?:$|[?#])/i.test(
          new URL(details.url).pathname,
        );
      } catch {
        isHeavyMedia = false;
      }
    }
    callback(isHeavyMedia ? { cancel: true } : {});
  });
  const userAgent = browserSession.getUserAgent().replace(/\sElectron\/\S+/i, "");
  browserSession.setUserAgent(userAgent);
}

function destroyRecord(record) {
  record.destroyed = true;
  clearLoadTimeout(record);
  releaseLoadingSlot(record);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.contentView.removeChildView(record.container);
  }
  record.container.removeChildView(record.view);
  if (!record.view.webContents.isDestroyed()) record.view.webContents.close();
  pumpLoadQueue();
}

function destroyAllViews() {
  for (const record of viewRecords.values()) destroyRecord(record);
  viewRecords.clear();
  activeOwnerKey = null;
}

function closePendingGoogleLogin() {
  if (!pendingGoogleLogin) return;
  clearTimeout(pendingGoogleLogin.timeout);
  pendingGoogleLogin.server.close();
  pendingGoogleLogin = null;
}

function sendGoogleResult(result) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(GOOGLE_AUTH_CHANNEL, result);
  }
}

async function startGoogleLogin() {
  closePendingGoogleLogin();
  const nonce = crypto.randomBytes(32).toString("base64url");
  const callbackPath = `/content-bot-google/${nonce}`;
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
    if (request.method !== "GET" || requestUrl.pathname !== callbackPath) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not found");
      return;
    }
    const accessToken = requestUrl.searchParams.get("auth_token") || "";
    const refreshToken = requestUrl.searchParams.get("refresh_token") || "";
    const authError = requestUrl.searchParams.get("auth_error") || "";
    const validTokens =
      accessToken.length > 20 &&
      accessToken.length <= 16_384 &&
      refreshToken.length > 20 &&
      refreshToken.length <= 16_384;
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    response.end(
      "<!doctype html><meta charset=utf-8><title>Content Bot</title>" +
        "<p>Đăng nhập đã hoàn tất. Bạn có thể đóng tab này và quay lại Content Bot.</p>",
    );
    closePendingGoogleLogin();
    if (authError) sendGoogleResult({ error: authError });
    else if (validTokens) sendGoogleResult({ accessToken, refreshToken });
    else sendGoogleResult({ error: "Auth Server không trả về phiên đăng nhập hợp lệ." });
    mainWindow?.focus();
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("Không tạo được cổng đăng nhập Desktop.");
  }
  const callbackUrl = `http://127.0.0.1:${address.port}${callbackPath}`;
  const authUrl = new URL("/auth/google", AUTH_SERVER_URL);
  authUrl.searchParams.set("desktop_callback", callbackUrl);
  const timeout = setTimeout(() => {
    closePendingGoogleLogin();
    sendGoogleResult({ error: "Đăng nhập Google đã hết thời gian chờ." });
  }, 5 * 60 * 1000);
  pendingGoogleLogin = { server, timeout };
  try {
    await shell.openExternal(authUrl.toString());
  } catch (error) {
    closePendingGoogleLogin();
    throw error;
  }
  return { started: true };
}

function navigationAllowed(record, candidateUrl) {
  return Boolean(safeChannelUrl(record.sourceId, candidateUrl));
}

async function explainExternalAuthRestriction(record) {
  if (!mainWindow || mainWindow.isDestroyed() || externalAuthNoticeOpen) return;
  externalAuthNoticeOpen = true;
  try {
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "Google không hỗ trợ đăng nhập trong app nhúng",
      message: "Không thể dùng “Tiếp tục với Google” bên trong Content Bot.",
      detail:
        "Google chặn OAuth trong trình duyệt nhúng. Hãy đặt mật khẩu X qua email rồi đăng nhập bằng email/username và mật khẩu; Content Bot sẽ giữ phiên X cho lần sau.",
      buttons: ["Đặt lại mật khẩu X", "Mở kênh bằng trình duyệt", "Đóng"],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    });
    if (response === 0 && !record.destroyed) {
      void record.view.webContents
        .loadURL("https://x.com/account/begin_password_reset")
        .catch(() => undefined);
    } else if (response === 1) {
      void shell.openExternal(record.url);
    }
  } finally {
    externalAuthNoticeOpen = false;
  }
}

function createRecord(channel, partition) {
  if (!mainWindow) throw new Error("Desktop window is not ready.");
  const container = new View();
  const view = new WebContentsView({
    webPreferences: {
      partition,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      safeDialogs: true,
      spellcheck: false,
      backgroundThrottling: true,
    },
  });
  view.setBackgroundColor("#ffffff");
  view.setBorderRadius(10);
  view.setVisible(false);
  container.setBackgroundColor("#ffffff");
  container.setBorderRadius(10);
  container.setVisible(false);
  container.addChildView(view);
  mainWindow.contentView.addChildView(container);

  const record = {
    channelId: channel.channelId,
    sourceId: channel.sourceId,
    url: channel.url,
    partition,
    container,
    view,
    desiredVisible: channel.visible,
    state: "queued",
    detail: "Đang chờ tải để tránh mở nhiều trang nặng cùng lúc…",
    queued: false,
    loadingSlot: false,
    loadTimeout: null,
    hasLoaded: false,
    destroyed: false,
  };
  configurePartition(record);
  view.webContents.setAudioMuted(true);

  view.webContents.on("did-start-loading", () => {
    if (record.state !== "ready") {
      sendStatus(record, "loading", "Đang tải kênh trong Content Bot Desktop…");
      applyVisibility(record);
    }
  });
  view.webContents.on("dom-ready", () => {
    if (!record.destroyed) {
      settleRecord(record, "ready", "Kênh đang hiển thị trong ứng dụng (video tự phát đã tắt)." );
    }
  });
  view.webContents.on("did-finish-load", () => {
    if (!record.destroyed && record.state !== "ready") {
      settleRecord(record, "ready", "Kênh đang hiển thị trong ứng dụng (video tự phát đã tắt)." );
    }
  });
  view.webContents.on(
    "did-fail-load",
    (_event, errorCode, errorDescription, _validatedUrl, isMainFrame) => {
      if (!isMainFrame || errorCode === -3) return;
      settleRecord(record, "error", `Không tải được kênh (${errorDescription}).`);
    },
  );
  view.webContents.on("render-process-gone", () => {
    settleRecord(record, "error", "Tiến trình hiển thị kênh đã dừng.");
  });
  view.webContents.on("will-navigate", (event) => {
    if (!navigationAllowed(record, event.url)) event.preventDefault();
  });
  view.webContents.setWindowOpenHandler(({ url }) => {
    if (safeAuthUrl(record.sourceId, url) && !navigationAllowed(record, url)) {
      void explainExternalAuthRestriction(record);
      return { action: "deny" };
    }
    if (navigationAllowed(record, url)) {
      void view.webContents.loadURL(url).catch(() => undefined);
    }
    return { action: "deny" };
  });
  return record;
}

function assertTrustedSender(event) {
  if (!mainWindow || event.sender !== mainWindow.webContents) {
    throw new Error("Desktop Live Wall request came from an untrusted renderer.");
  }
}

function syncViews(payload) {
  const normalized = normalizeSyncPayload(payload);
  const nextOwnerKey = ownerKey(normalized.ownerId);
  if (activeOwnerKey && activeOwnerKey !== nextOwnerKey) destroyAllViews();
  activeOwnerKey = nextOwnerKey;
  const partition = `persist:content-bot-live-wall-${nextOwnerKey}`;
  const wanted = new Set(normalized.views.map((view) => view.channelId));

  for (const [channelId, record] of viewRecords) {
    if (!wanted.has(channelId)) {
      destroyRecord(record);
      viewRecords.delete(channelId);
    }
  }

  for (const channel of normalized.views) {
    let record = viewRecords.get(channel.channelId);
    if (
      record &&
      (record.url !== channel.url ||
        record.sourceId !== channel.sourceId ||
        record.partition !== partition)
    ) {
      destroyRecord(record);
      viewRecords.delete(channel.channelId);
      record = undefined;
    }
    if (!record) {
      record = createRecord(channel, partition);
      viewRecords.set(channel.channelId, record);
    }
    const contentBounds = mainWindow.getContentBounds();
    const clipped = clippedViewBounds(channel.bounds, {
      width: contentBounds.width,
      height: contentBounds.height,
    });
    record.desiredVisible = channel.visible && Boolean(clipped);
    if (clipped) {
      record.container.setBounds(clipped.container);
      record.view.setBounds(clipped.content);
    }
    if (!record.hasLoaded && !record.queued && !record.loadingSlot) enqueueLoad(record);
    applyVisibility(record);
  }
  return normalized.views.map((view) => statusOf(viewRecords.get(view.channelId)));
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 960,
    minHeight: 700,
    show: true,
    title: "Content Bot Desktop Dev",
    backgroundColor: "#f4f6f8",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event) => {
    let allowed = false;
    try {
      allowed = new URL(event.url).origin === new URL(DEV_URL).origin;
    } catch {
      allowed = false;
    }
    if (!allowed) event.preventDefault();
  });
  mainWindow.webContents.on("did-finish-load", () => {
    console.log(`[desktop] Content Bot loaded from ${DEV_URL}.`);
  });
  mainWindow.webContents.on(
    "did-fail-load",
    (_event, errorCode, errorDescription, validatedUrl, isMainFrame) => {
      if (isMainFrame && errorCode !== -3) {
        console.error(
          `[desktop] Failed to load ${validatedUrl}: ${errorDescription} (${errorCode}).`,
        );
      }
    },
  );
  mainWindow.on("closed", () => {
    console.log("[desktop] Desktop window closed.");
    destroyAllViews();
    mainWindow = null;
  });
  void mainWindow.loadURL(DEV_URL);
}

ipcMain.handle("content-bot:desktop-environment", (event) => {
  assertTrustedSender(event);
  return { available: true, platform: process.platform, version: app.getVersion() };
});
ipcMain.handle("content-bot:desktop-live-wall-sync", (event, payload) => {
  assertTrustedSender(event);
  return syncViews(payload);
});
ipcMain.handle("content-bot:desktop-live-wall-close", (event) => {
  assertTrustedSender(event);
  destroyAllViews();
  return { closed: true };
});
ipcMain.handle("content-bot:desktop-live-wall-reload", (event, channelId) => {
  assertTrustedSender(event);
  const id = String(channelId || "");
  const record = viewRecords.get(id);
  if (!record) return { reloaded: false };
  clearLoadTimeout(record);
  releaseLoadingSlot(record);
  record.queued = false;
  // Return to the configured channel URL while preserving cookies from any
  // login completed inside this persistent Electron partition.
  record.hasLoaded = false;
  enqueueLoad(record);
  return { reloaded: true };
});
ipcMain.handle("content-bot:desktop-live-wall-login-x", (event, channelId) => {
  assertTrustedSender(event);
  const id = String(channelId || "");
  const record = viewRecords.get(id);
  if (!record || record.sourceId !== "x" || record.destroyed) {
    return { started: false };
  }
  clearLoadTimeout(record);
  releaseLoadingSlot(record);
  record.queued = false;
  record.hasLoaded = true;
  sendStatus(record, "loading", "Đang mở trang đăng nhập X trong Content Bot…");
  applyVisibility(record);
  record.loadTimeout = setTimeout(() => {
    if (!record.destroyed && record.state !== "ready") {
      settleRecord(record, "error", "Trang đăng nhập X tải quá lâu. Hãy thử lại.");
    }
  }, 25_000);
  void record.view.webContents.loadURL(X_LOGIN_URL).catch((error) => {
    if (!record.destroyed) {
      settleRecord(record, "error", `Không mở được đăng nhập X (${error.message}).`);
    }
  });
  return { started: true };
});
ipcMain.handle("content-bot:desktop-google-auth-start", async (event) => {
  assertTrustedSender(event);
  return startGoogleLogin();
});
ipcMain.handle("content-bot:desktop-download-file", (event, payload) => {
  assertTrustedSender(event);
  const download = safeSubtitleDownload(payload?.url, payload?.filename);
  if (!download) throw new Error("Yêu cầu tải video không hợp lệ.");
  const accessToken = String(payload?.accessToken || "");
  if (accessToken.length > 16_384 || /[\r\n]/.test(accessToken)) {
    throw new Error("Phiên đăng nhập tải video không hợp lệ.");
  }
  const browserSession = event.sender.session;
  return new Promise((resolve, reject) => {
    const onDownload = (_downloadEvent, item, webContents) => {
      if (webContents !== event.sender) return;
      item.setSaveDialogOptions({
        defaultPath: download.filename,
        filters: [{ name: "Video MP4", extensions: ["mp4"] }],
      });
      item.once("done", (_itemEvent, state) => {
        resolve({
          state,
          ...(item.getSavePath() ? { savePath: item.getSavePath() } : {}),
        });
      });
    };
    browserSession.once("will-download", onDownload);
    try {
      event.sender.downloadURL(download.url, {
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      });
    } catch (error) {
      browserSession.removeListener("will-download", onDownload);
      reject(error);
    }
  });
});

if (hasSingleInstanceLock) {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.whenReady().then(() => {
    createMainWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  closePendingGoogleLogin();
  destroyAllViews();
});
