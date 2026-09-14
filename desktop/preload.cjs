const { contextBridge, ipcRenderer } = require("electron");

const STATUS_CHANNEL = "content-bot:desktop-live-wall-status";
const GOOGLE_AUTH_CHANNEL = "content-bot:desktop-google-auth-result";

contextBridge.exposeInMainWorld("contentBotDesktop", {
  environment: () => ipcRenderer.invoke("content-bot:desktop-environment"),
  pickProjectVideo: (payload) => ipcRenderer.invoke("content-bot:desktop-pick-project-video", payload),
  syncLiveWall: (payload) => ipcRenderer.invoke("content-bot:desktop-live-wall-sync", payload),
  closeLiveWall: () => ipcRenderer.invoke("content-bot:desktop-live-wall-close"),
  reloadLiveWallView: (channelId) =>
    ipcRenderer.invoke("content-bot:desktop-live-wall-reload", channelId),
  loginXLiveWallView: (channelId) =>
    ipcRenderer.invoke("content-bot:desktop-live-wall-login-x", channelId),
  downloadFile: (payload) =>
    ipcRenderer.invoke("content-bot:desktop-download-file", payload),
  onLiveWallStatus: (callback) => {
    if (typeof callback !== "function") return () => undefined;
    const listener = (_event, status) => callback(status);
    ipcRenderer.on(STATUS_CHANNEL, listener);
    return () => ipcRenderer.removeListener(STATUS_CHANNEL, listener);
  },
  startGoogleLogin: () => ipcRenderer.invoke("content-bot:desktop-google-auth-start"),
  onGoogleAuthResult: (callback) => {
    if (typeof callback !== "function") return () => undefined;
    const listener = (_event, result) => callback(result);
    ipcRenderer.on(GOOGLE_AUTH_CHANNEL, listener);
    return () => ipcRenderer.removeListener(GOOGLE_AUTH_CHANNEL, listener);
  },
});
