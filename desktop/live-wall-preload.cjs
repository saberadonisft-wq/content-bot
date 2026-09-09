// Preload script for Live Wall embedded views.
// Runs BEFORE any page JavaScript, patching properties that platforms
// like X use to detect Electron/embedded browsers.
// Safety: sandbox=true + nodeIntegration=false prevent Node.js access;
// contextIsolation is disabled only for these third-party view pages
// so this script shares the page JS context and can patch globals early.

Object.defineProperty(navigator, "webdriver", {
  get: () => false,
  configurable: true,
});

Object.defineProperty(navigator, "languages", {
  get: () => ["vi-VN", "vi", "en-US", "en"],
  configurable: true,
});

Object.defineProperty(navigator, "plugins", {
  get: () => {
    const arr = [
      { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1 },
      { name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1 },
      { name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1 },
    ];
    arr.item = (i) => arr[i];
    arr.namedItem = (n) => arr.find((p) => p.name === n);
    arr.refresh = () => {};
    return arr;
  },
  configurable: true,
});

Object.defineProperty(navigator, "mimeTypes", {
  get: () => {
    const arr = [{ type: "application/pdf", suffixes: "pdf", description: "Portable Document Format" }];
    arr.item = (i) => arr[i];
    arr.namedItem = (n) => arr.find((m) => m.type === n);
    return arr;
  },
  configurable: true,
});

if (!window.chrome) {
  window.chrome = {
    app: { isInstalled: false, InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" }, RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" } },
    runtime: { OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {}, PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {} },
    loadTimes: () => ({}),
    csi: () => ({}),
  };
}

// Notification.permission is "default" in Electron but "denied" in most
// embedded contexts; some detection scripts check this.
try {
  Object.defineProperty(Notification, "permission", {
    get: () => "default",
    configurable: true,
  });
} catch (_) {}
