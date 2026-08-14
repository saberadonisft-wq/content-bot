import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const port = 9333;
const appUrl = process.env.SUBTITLE_AUDIT_URL ?? "http://127.0.0.1:5173/?view=subtitles";
const videoId = process.env.SUBTITLE_AUDIT_VIDEO_ID ?? null;
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    };
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

const evaluate = async (client, expression) => {
  const result = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
  }
  return result.result.value;
};

const text = "KHẢ KHẢ, TRÀ TRÀ, SAO CÁC CẬU LẠI...";
const draft = {
  version: 2,
  videoId,
  projectName: "Đo đồng bộ preview/render",
  mediaDurationMs: 5000,
  rawText: "",
  cues: [{
    id: "sync-1",
    start_ms: 0,
    end_ms: 5000,
    text,
    timing_source: "manual",
    timing_precision_ms: 1,
    confidence: null,
    needs_review: false,
    revision: 0,
  }],
  selectedCueId: null,
  activeAlignmentJobId: null,
  activeRenderJobId: null,
  options: {
    font_name: "Arimo",
    font_size: 38,
    font_color: "#FFFFFF",
    bold: true,
    italic: false,
    underline: false,
    strikethrough: false,
    uppercase: false,
    alignment_type: "center",
    outline_color: "#000000",
    outline_width: 2,
    shadow_color: "#000000",
    shadow_width: 2,
    bg_enabled: true,
    bg_color: "#000000",
    bg_opacity: 0.75,
    spacing: 0,
    line_spacing: 1.2,
    pos_x: 50,
    pos_y: 78,
    position: "custom",
    video_speed: 1,
    volume: 1,
    fade_in: 0,
    fade_out: 0,
    aspect_ratio: "original",
    bg_fill_type: "blur",
    trim_start: 0,
    trim_end: null,
    animation: "none",
  },
  overlayLayout: { x: 14, y: 12, width: 22 },
};

const profileDirectory = await mkdtemp(path.join(os.tmpdir(), "content-bot-subtitle-sync-"));
const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDirectory}`,
    "--no-first-run",
    "--disable-default-apps",
    "--disable-background-networking",
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true },
);

try {
  let page;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
      page = pages.find((item) => item.type === "page");
      if (page) break;
    } catch {
      // Chrome is still starting.
    }
    await delay(100);
  }
  if (!page) throw new Error("Chrome DevTools endpoint did not become ready.");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  const client = new CdpClient(socket);
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await client.send("Page.navigate", { url: appUrl });
  await delay(1000);
  await evaluate(
    client,
    `localStorage.setItem("content-bot:subtitle-studio:v2", ${JSON.stringify(JSON.stringify(draft))}); location.reload(); true`,
  );
  await delay(2200);
  const metrics = await evaluate(client, `(() => {
    const frame = document.querySelector('.preview-media-frame');
    const overlay = document.querySelector('.preview-subtitle-overlay');
    const copy = document.querySelector('.preview-subtitle-copy');
    if (!frame || !overlay || !copy) return null;
    const frameRect = frame.getBoundingClientRect();
    const overlayRect = overlay.getBoundingClientRect();
    const copyRect = copy.getBoundingClientRect();
    const style = getComputedStyle(overlay);
    const libassCanvas = document.querySelector('canvas.JASSUB');
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    context.font = [style.fontStyle, style.fontWeight, style.fontSize, style.fontFamily].join(' ');
    const textMetrics = context.measureText(copy.textContent ?? '');
    return {
      text: copy.textContent,
      frame: { width: frameRect.width, height: frameRect.height },
      overlay: { width: overlayRect.width, height: overlayRect.height },
      copy: { width: copyRect.width, height: copyRect.height },
      normalized: {
        overlayWidth: overlayRect.width / frameRect.width,
        overlayHeight: overlayRect.height / frameRect.height,
        copyWidth: copyRect.width / frameRect.width,
        copyHeight: copyRect.height / frameRect.height,
      },
      computed: {
        fontFamily: style.fontFamily,
        fontSize: style.fontSize,
        fontWeight: style.fontWeight,
        padding: style.padding,
        lineHeight: style.lineHeight,
        strokeWidth: style.webkitTextStrokeWidth,
      },
      paintedText: {
        width: textMetrics.actualBoundingBoxLeft + textMetrics.actualBoundingBoxRight,
        height: textMetrics.actualBoundingBoxAscent + textMetrics.actualBoundingBoxDescent,
        ascent: textMetrics.actualBoundingBoxAscent,
        descent: textMetrics.actualBoundingBoxDescent,
      },
      fontsReady: document.fonts.status,
      libass: libassCanvas ? {
        active: true,
        width: libassCanvas.width,
        height: libassCanvas.height,
        cssWidth: libassCanvas.getBoundingClientRect().width,
        cssHeight: libassCanvas.getBoundingClientRect().height,
      } : { active: false },
    };
  })()`);
  if (!metrics) throw new Error("Không đo được phụ đề preview.");
  console.log(JSON.stringify(metrics, null, 2));
  socket.close();
} finally {
  chrome.kill();
  await delay(500);
  await rm(profileDirectory, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 200,
  });
}
