import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const repository = path.resolve(import.meta.dirname, "..", "..");
const outputDirectory = path.join(repository, "data", "subtitle-ui-audit");
const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const port = 9331;
const videoId = process.argv[2];
if (!videoId) throw new Error("Pass the uploaded visual fixture video ID as the first argument.");

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

const cue = (index, count) => {
  const start = Math.round((index * 118_000) / Math.max(1, count));
  const duration = 180 + (index % 5) * 75;
  return {
    id: `audit-${String(index + 1).padStart(4, "0")}`,
    start_ms: start,
    end_ms: start + duration,
    text: [
      "Chúng ta bắt đầu ngay bây giờ.",
      "Timing được giữ chính xác đến mili-giây.",
      "Kéo cue để kiểm tra hiệu năng timeline.",
      "Phụ đề tiếng Việt cần rõ ràng và dễ đọc.",
    ][index % 4],
    timing_source: index % 3 === 0 ? "manual" : "forced_alignment",
    timing_precision_ms: index % 3 === 0 ? 1 : 10,
    confidence: index % 3 === 0 ? null : 0.86,
    needs_review: index % 17 === 0,
    revision: 0,
  };
};

const draft = (count) => ({
  version: 2,
  videoId,
  projectName: `Kiểm định giao diện · ${count} cue`,
  mediaDurationMs: 120_000,
  rawText: "Dữ liệu kiểm định tự động cho Subtitle Studio.",
  cues: Array.from({ length: count }, (_, index) => cue(index, count)),
  selectedCueId: "audit-0001",
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
    bg_enabled: false,
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
});

const evaluate = async (client, expression) => {
  const result = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
};

await mkdir(outputDirectory, { recursive: true });
const profileDirectory = await mkdtemp(path.join(os.tmpdir(), "content-bot-ui-audit-"));
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
      const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) =>
        response.json(),
      );
      page = pages.find((item) => item.type === "page");
      if (page) break;
    } catch {
      // Chrome is still starting.
    }
    await delay(100);
  }
  if (!page) throw new Error("Chrome DevTools endpoint did not start.");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  const client = new CdpClient(socket);
  await Promise.all([
    client.send("Page.enable"),
    client.send("Runtime.enable"),
    client.send("Performance.enable"),
    client.send("Accessibility.enable"),
  ]);
  await client.send("Page.navigate", { url: "http://127.0.0.1:5173/?view=subtitles" });
  await delay(1200);
  await evaluate(
    client,
    `localStorage.setItem("content-bot:subtitle-studio:v2", ${JSON.stringify(
      JSON.stringify(draft(40)),
    )}); location.reload(); true`,
  );
  await delay(2200);

  const viewports = [
    [1440, 900],
    [1366, 768],
    [1024, 768],
    [768, 1024],
    [414, 896],
    [390, 844],
    [375, 812],
    [320, 800],
  ];
  const viewportResults = [];
  for (const [width, height] of viewports) {
    await client.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 600,
    });
    if (width < 600) {
      await evaluate(
        client,
        `(() => { const panel = document.querySelector('.subtitle-tool-panel'); if (panel && !panel.classList.contains('is-open')) document.querySelector('.is-sidebar-toggle')?.click(); return true; })()`,
      );
    }
    await delay(350);
    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    const filename = `subtitle-studio-${width}x${height}.png`;
    await writeFile(path.join(outputDirectory, filename), Buffer.from(screenshot.data, "base64"));
    const measurements = await evaluate(
      client,
      `(() => ({
        width: innerWidth,
        height: innerHeight,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
        timelineCueNodes: document.querySelectorAll('.subtitle-timeline-cue').length,
        sidebarCueNodes: document.querySelectorAll('.subtitle-cue-editor').length,
        stage: (() => { const node = document.querySelector('.preview-stage'); if (!node) return null; const r = node.getBoundingClientRect(); return { width: Math.round(r.width), height: Math.round(r.height), top: Math.round(r.top) }; })(),
        errorVisible: Boolean(document.querySelector('.subtitle-studio-error')),
      }))()`,
    );
    viewportResults.push({ filename, ...measurements });
    if (width === 390 || width === 320) {
      await evaluate(client, `document.querySelector('.is-sidebar-toggle')?.click(); true`);
      await delay(300);
      const workspaceScreenshot = await client.send("Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: false,
      });
      const workspaceFilename = `subtitle-studio-${width}x${height}-workspace.png`;
      await writeFile(
        path.join(outputDirectory, workspaceFilename),
        Buffer.from(workspaceScreenshot.data, "base64"),
      );
      viewportResults.push({
        filename: workspaceFilename,
        ...(await evaluate(
          client,
          `({ width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight, timelineCueNodes: document.querySelectorAll('.subtitle-timeline-cue').length, sidebarCueNodes: document.querySelectorAll('.subtitle-cue-editor').length, stage: (() => { const node = document.querySelector('.preview-stage'); if (!node) return null; const r = node.getBoundingClientRect(); return { width: Math.round(r.width), height: Math.round(r.height), top: Math.round(r.top) }; })(), errorVisible: Boolean(document.querySelector('.subtitle-studio-error')) })`,
        )),
      });
    }
  }

  await client.send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await evaluate(
    client,
    `(() => { const panel = document.querySelector('.subtitle-tool-panel'); if (panel && !panel.classList.contains('is-open')) document.querySelector('.is-sidebar-toggle')?.click(); return true; })()`,
  );
  await delay(100);

  const contrast = await evaluate(
    client,
    `(() => {
      const canvas = document.createElement('canvas');
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      const rgba = (color) => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = 'rgba(0, 0, 0, 0)';
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
        return [r / 255, g / 255, b / 255, a / 255];
      };
      const over = (foreground, background) => {
        const alpha = foreground[3] + background[3] * (1 - foreground[3]);
        if (alpha <= 0) return [1, 1, 1, 1];
        return [
          (foreground[0] * foreground[3] + background[0] * background[3] * (1 - foreground[3])) / alpha,
          (foreground[1] * foreground[3] + background[1] * background[3] * (1 - foreground[3])) / alpha,
          (foreground[2] * foreground[3] + background[2] * background[3] * (1 - foreground[3])) / alpha,
          alpha,
        ];
      };
      const luminance = (color) => {
        const linear = color.slice(0, 3).map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
      };
      const ratio = (left, right) => {
        const a = luminance(left);
        const b = luminance(right);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      };
      const backgroundFor = (element) => {
        const chain = [];
        for (let node = element; node instanceof Element; node = node.parentElement) chain.push(node);
        let background = [1, 1, 1, 1];
        for (const node of chain.reverse()) background = over(rgba(getComputedStyle(node).backgroundColor), background);
        return background;
      };
      const name = (element) => {
        const classes = [...element.classList].slice(0, 2).join('.');
        return element.tagName.toLowerCase() + (classes ? '.' + classes : '');
      };
      const root = document.querySelector('.subtitle-studio-shell');
      const candidates = [...root.querySelectorAll('*')].filter((element) => {
        if (element.closest('.preview-media-frame')) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 || rect.width <= 0 || rect.height <= 0) return false;
        const directText = [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
        return directText || ['BUTTON', 'A', 'INPUT', 'TEXTAREA', 'SELECT', 'SUMMARY'].includes(element.tagName);
      });
      const failures = [];
      let minimumRatio = Infinity;
      for (const element of candidates) {
        const style = getComputedStyle(element);
        const background = backgroundFor(element);
        const foreground = over(rgba(style.color), background);
        const measured = ratio(foreground, background);
        const fontSize = Number.parseFloat(style.fontSize);
        const fontWeight = Number.parseInt(style.fontWeight, 10) || 400;
        const threshold = fontSize >= 24 || (fontSize >= 18 && fontWeight >= 700) ? 3 : 4.5;
        minimumRatio = Math.min(minimumRatio, measured);
        if (measured + 0.01 < threshold) failures.push({ element: name(element), ratio: Number(measured.toFixed(2)), threshold, text: (element.textContent || element.getAttribute('aria-label') || '').trim().slice(0, 80) });
      }
      return { checkedNodes: candidates.length, minimumRatio: Number(minimumRatio.toFixed(2)), failures: failures.slice(0, 30) };
    })()`,
  );

  const editorWorkflow = await evaluate(
    client,
    `(async () => {
      const waitFor = async (predicate, timeoutMs = 6000) => {
        const deadline = performance.now() + timeoutMs;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
        return false;
      };
      const panel = document.querySelector('.subtitle-tool-panel');
      if (panel && !panel.classList.contains('is-open')) {
        document.querySelector('.is-sidebar-toggle')?.click();
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      const textarea = document.querySelector('.studio-textarea-field textarea');
      const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      valueSetter?.call(textarea, '[00:00:00.001 --> 00:00:00.999] Xin chào chính xác mili-giây.');
      textarea?.dispatchEvent(new Event('input', { bubbles: true }));
      const parseButton = [...document.querySelectorAll('button')].find((button) => button.textContent?.includes('Phân tích phụ đề'));
      parseButton?.click();
      const parsed = await waitFor(() => document.querySelectorAll('.subtitle-cue-editor').length === 1 && Boolean(document.querySelector('.subtitle-time-input')));
      const timecodes = [...document.querySelectorAll('.subtitle-time-input')].slice(0, 2).map((input) => input.value);
      const count = () => document.querySelectorAll('.subtitle-cue-editor').length;
      document.querySelector('button[aria-label="Tách cue tại playhead"]')?.click();
      await waitFor(() => count() === 2);
      const afterSplit = count();
      document.querySelector('button[aria-label="Hoàn tác"]')?.click();
      await waitFor(() => count() === 1);
      const afterUndo = count();
      document.querySelector('button[aria-label="Làm lại"]')?.click();
      await waitFor(() => count() === 2);
      const afterRedo = count();
      document.querySelector('button[aria-label="Gộp cue kế tiếp"]')?.click();
      await waitFor(() => count() === 1);
      return {
        parsed,
        timecodes,
        afterSplit,
        afterUndo,
        afterRedo,
        afterMerge: count(),
        errorVisible: Boolean(document.querySelector('.subtitle-studio-error')),
      };
    })()`,
  );

  const accessibility = await client.send("Accessibility.getFullAXTree");
  const unnamedInteractiveNodes = accessibility.nodes
    .filter((node) => ["button", "link", "textbox", "combobox"].includes(node.role?.value))
    .filter((node) => !String(node.name?.value ?? "").trim())
    .map((node) => ({ role: node.role?.value, backendDOMNodeId: node.backendDOMNodeId }));

  await evaluate(
    client,
    `localStorage.setItem("content-bot:subtitle-studio:v2", ${JSON.stringify(
      JSON.stringify(draft(500)),
    )}); location.reload(); true`,
  );
  await delay(2200);
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (await evaluate(client, `Boolean(document.querySelector('.video-sprite-frame'))`)) break;
    await delay(100);
  }
  await evaluate(
    client,
    `(async () => {
      await document.fonts.ready;
      const frame = document.querySelector('.video-sprite-frame');
      const match = frame ? getComputedStyle(frame).backgroundImage.match(/^url\\(["']?(.*?)["']?\\)$/) : null;
      if (match?.[1]) {
        const image = new Image();
        image.src = match[1];
        await image.decode();
      }
      const video = document.querySelector('.preview-media-frame video');
      if (video && video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        await Promise.race([
          new Promise((resolve) => video.addEventListener('loadeddata', resolve, { once: true })),
          new Promise((resolve) => setTimeout(resolve, 2000)),
        ]);
      }
      await new Promise((resolve) =>
        'requestIdleCallback' in window
          ? requestIdleCallback(resolve, { timeout: 2000 })
          : setTimeout(resolve, 100),
      );
      for (let index = 0; index < 30; index += 1) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      return true;
    })()`,
  );
  await evaluate(
    client,
    `window.__subtitleLongTasks = []; new PerformanceObserver((list) => window.__subtitleLongTasks.push(...list.getEntries().map((entry) => ({ startTime: entry.startTime, duration: entry.duration })))).observe({ type: 'longtask' }); true`,
  );
  const idleResult = await evaluate(
    client,
    `(async () => {
      window.__subtitleLongTasks = [];
      const start = performance.now();
      const frameTimes = [start];
      for (let index = 0; index < 90; index += 1) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
        frameTimes.push(performance.now());
      }
      const elapsedMs = performance.now() - start;
      const frameGaps = frameTimes.slice(1).map((time, index) => time - frameTimes[index]);
      await new Promise((resolve) => setTimeout(resolve, 0));
      return {
        elapsedMs,
        fps: 90 / (elapsedMs / 1000),
        maxFrameGapMs: Math.max(...frameGaps),
        longTasks: window.__subtitleLongTasks,
      };
    })()`,
  );
  await evaluate(client, `window.__subtitleLongTasks = []; true`);
  const dragRect = await evaluate(
    client,
    `(() => { const node = document.querySelector('.subtitle-timeline-cue'); if (!node) return null; const r = node.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`,
  );
  const beforeMetrics = await client.send("Performance.getMetrics");
  const steps = 90;
  const dragResult = dragRect
    ? await evaluate(
        client,
        `(async () => {
          const node = document.querySelector('.subtitle-timeline-cue');
          const start = performance.now();
          const frameTimes = [start];
          let snapObserved = false;
          let snapLabel = '';
          node.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 7, button: 0, buttons: 1, clientX: ${dragRect.x}, clientY: ${dragRect.y} }));
          for (let index = 1; index <= ${steps}; index += 1) {
            await new Promise((resolve) => requestAnimationFrame(resolve));
            frameTimes.push(performance.now());
            const guide = document.querySelector('.timeline-snap-guide');
            if (guide && !guide.hidden) {
              snapObserved = true;
              snapLabel = guide.textContent?.trim() ?? snapLabel;
            }
            window.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 7, button: 0, buttons: 1, clientX: ${dragRect.x} + index * 180 / ${steps}, clientY: ${dragRect.y} }));
          }
          window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 7, button: 0, buttons: 0, clientX: ${dragRect.x + 180}, clientY: ${dragRect.y} }));
          const elapsedMs = performance.now() - start;
          const frameGaps = frameTimes.slice(1).map((time, index) => time - frameTimes[index]);
          return { startTime: start, elapsedMs, fps: ${steps} / (elapsedMs / 1000), maxFrameGapMs: Math.max(...frameGaps), maxFrameGapIndex: frameGaps.indexOf(Math.max(...frameGaps)), snapObserved, snapLabel };
        })()`,
      )
    : { elapsedMs: 0, fps: 0 };
  const afterMetrics = await client.send("Performance.getMetrics");
  const taskDuration = (metrics) =>
    metrics.metrics.find((metric) => metric.name === "TaskDuration")?.value ?? 0;
  const performanceResult = await evaluate(
    client,
    `({
      timelineCueNodes: document.querySelectorAll('.subtitle-timeline-cue').length,
      sidebarCueNodes: document.querySelectorAll('.subtitle-cue-editor').length,
      longTasksMs: window.__subtitleLongTasks ?? [],
      totalCues: JSON.parse(localStorage.getItem('content-bot:subtitle-studio:v2')).cues.length,
    })`,
  );
  performanceResult.dragElapsedMs = Math.round(dragResult.elapsedMs);
  performanceResult.dragStartTime = Math.round(dragResult.startTime ?? 0);
  performanceResult.animationFps = Number(dragResult.fps.toFixed(1));
  performanceResult.maxFrameGapMs = Number((dragResult.maxFrameGapMs ?? 0).toFixed(1));
  performanceResult.maxFrameGapIndex = dragResult.maxFrameGapIndex ?? -1;
  performanceResult.snapObserved = Boolean(dragResult.snapObserved);
  performanceResult.snapLabel = dragResult.snapLabel ?? "";
  performanceResult.mainThreadTaskMs = Number(
    ((taskDuration(afterMetrics) - taskDuration(beforeMetrics)) * 1000).toFixed(1),
  );

  const report = {
    generatedAt: new Date().toISOString(),
    viewports: viewportResults,
    editorWorkflow,
    accessibility: { unnamedInteractiveNodes },
    contrast,
    idleAnimation: {
      elapsedMs: Math.round(idleResult.elapsedMs),
      fps: Number(idleResult.fps.toFixed(1)),
      maxFrameGapMs: Number(idleResult.maxFrameGapMs.toFixed(1)),
      longTasksMs: idleResult.longTasks,
    },
    performance500Cues: performanceResult,
  };
  await writeFile(
    path.join(outputDirectory, "audit.json"),
    `${JSON.stringify(report, null, 2)}\n`,
  );
  console.log(JSON.stringify(report, null, 2));
  socket.close();
} finally {
  chrome.kill();
  await delay(1000);
  try {
    await rm(profileDirectory, { recursive: true, force: true, maxRetries: 4, retryDelay: 250 });
  } catch {
    // Chrome Crashpad may briefly retain a handle; the OS temp directory can clean it later.
  }
}
