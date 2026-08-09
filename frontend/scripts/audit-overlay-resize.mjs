import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const repository = path.resolve(import.meta.dirname, "..", "..");
const outputDirectory = path.join(repository, "data", "subtitle-ui-audit");
const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const port = 9332;
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
    throw new Error(
      result.exceptionDetails.exception?.description ??
        result.exceptionDetails.exception?.value ??
        result.exceptionDetails.text,
    );
  }
  return result.result.value;
};

const drag = async (client, selector, deltaX, deltaY) =>
  evaluate(
    client,
    `(async () => {
      const node = document.querySelector(${JSON.stringify(selector)});
      if (!node) throw new Error('Không tìm thấy vùng kéo ${selector}');
      const rect = node.getBoundingClientRect();
      const startX = rect.left + rect.width / 2;
      const startY = rect.top + rect.height / 2;
      const pointerId = 17;
      node.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        pointerId,
        pointerType: 'mouse',
        button: 0,
        buttons: 1,
        clientX: startX,
        clientY: startY,
      }));
      for (let index = 1; index <= 8; index += 1) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
        window.dispatchEvent(new PointerEvent('pointermove', {
          bubbles: true,
          pointerId,
          pointerType: 'mouse',
          button: 0,
          buttons: 1,
          clientX: startX + (${deltaX} * index) / 8,
          clientY: startY + (${deltaY} * index) / 8,
        }));
      }
      window.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true,
        pointerId,
        pointerType: 'mouse',
        button: 0,
        buttons: 0,
        clientX: startX + ${deltaX},
        clientY: startY + ${deltaY},
      }));
      await new Promise((resolve) => requestAnimationFrame(resolve));
      return true;
    })()`,
  );

await mkdir(outputDirectory, { recursive: true });
const profileDirectory = await mkdtemp(path.join(os.tmpdir(), "content-bot-overlay-audit-"));
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
  await client.send("Page.navigate", { url: "http://127.0.0.1:5173/?view=subtitles" });
  await delay(800);
  await evaluate(client, `localStorage.removeItem("content-bot:subtitle-studio:v2"); location.reload(); true`);
  await delay(2200);
  await evaluate(
    client,
    `document.querySelector('.subtitle-tool-rail button[aria-label="Tải lên"]')?.click(); true`,
  );
  await delay(500);
  await evaluate(client, `(async () => {
    const input = document.querySelector('input[accept*=".png"]');
    if (!input) throw new Error('Không tìm thấy input ảnh phủ');
    const canvas = document.createElement('canvas');
    canvas.width = 800;
    canvas.height = 400;
    const context = canvas.getContext('2d');
    context.fillStyle = '#2563eb';
    context.fillRect(0, 0, 800, 400);
    context.fillStyle = '#ffffff';
    context.font = 'bold 120px Arial';
    context.textAlign = 'center';
    context.fillText('OVERLAY', 400, 245);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    if (!blob) throw new Error('Không tạo được PNG kiểm thử');
    const file = new File([blob], 'overlay-audit.png', { type: 'image/png' });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    Object.defineProperty(input, 'files', { configurable: true, value: transfer.files });
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`);

  let geometry = null;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    geometry = await evaluate(client, `(() => {
      const overlay = document.querySelector('.preview-image-overlay');
      const handle = document.querySelector('.preview-image-resize-handle.is-se');
      if (!overlay || !handle || !overlay.querySelector('img')?.complete || !overlay.classList.contains('is-selected')) return null;
      const rect = overlay.getBoundingClientRect();
      const handleRect = handle.getBoundingClientRect();
      return {
        width: rect.width,
        center: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
        handle: { x: handleRect.left + handleRect.width / 2, y: handleRect.top + handleRect.height / 2 },
        selected: overlay.classList.contains('is-selected'),
      };
    })()`);
    if (geometry) break;
    await delay(100);
  }
  if (!geometry) throw new Error("Ảnh phủ không xuất hiện trong preview.");

  let persistedOverlayId = null;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    persistedOverlayId = await evaluate(
      client,
      `JSON.parse(localStorage.getItem('content-bot:subtitle-studio:v2') || '{}').overlayId || null`,
    );
    if (/^[a-f0-9]{64}$/.test(persistedOverlayId ?? '')) break;
    await delay(100);
  }
  if (!/^[a-f0-9]{64}$/.test(persistedOverlayId ?? '')) {
    throw new Error("Ảnh phủ chưa được đồng bộ với backend.");
  }

  await drag(client, ".preview-image-resize-handle.is-se", 120, 60);
  const afterResize = await evaluate(client, `(() => {
    const overlay = document.querySelector('.preview-image-overlay');
    const rect = overlay.getBoundingClientRect();
    const slider = document.querySelector('input[aria-label^="Kích thước ảnh phủ"]');
    return {
      width: rect.width,
      center: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      sliderValue: Number(slider.value),
      label: overlay.querySelector('.preview-image-size-label')?.value,
    };
  })()`);
  if (afterResize.width <= geometry.width + 20) {
    throw new Error(`Resize không tăng chiều rộng: ${geometry.width} -> ${afterResize.width}`);
  }

  await drag(client, ".preview-image-overlay", 80, 40);
  const afterMove = await evaluate(client, `(() => {
    const overlay = document.querySelector('.preview-image-overlay');
    const rect = overlay.getBoundingClientRect();
    const saved = JSON.parse(localStorage.getItem('content-bot:subtitle-studio:v2') || '{}');
    return {
      center: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      layout: saved.overlayLayout || null,
      errorVisible: Boolean(document.querySelector('.subtitle-studio-error')),
    };
  })()`);
  if (afterMove.center.x <= afterResize.center.x + 30 || afterMove.center.y <= afterResize.center.y + 15) {
    throw new Error("Kéo vị trí ảnh phủ không cập nhật đủ khoảng cách.");
  }
  await delay(700);
  const persistedLayout = await evaluate(
    client,
    `JSON.parse(localStorage.getItem('content-bot:subtitle-studio:v2') || '{}').overlayLayout || null`,
  );
  if (!persistedLayout) throw new Error("Bố cục ảnh phủ chưa được lưu vào draft.");

  const screenshot = await client.send("Page.captureScreenshot", { format: "png" });
  const screenshotPath = path.join(outputDirectory, "overlay-resize-desktop.png");
  await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  const report = {
    selectedInitially: geometry.selected,
    widthBefore: Math.round(geometry.width * 10) / 10,
    widthAfter: Math.round(afterResize.width * 10) / 10,
    sliderValue: afterResize.sliderValue,
    sizeLabel: afterResize.label,
    movedBy: {
      x: Math.round((afterMove.center.x - afterResize.center.x) * 10) / 10,
      y: Math.round((afterMove.center.y - afterResize.center.y) * 10) / 10,
    },
    persistedLayout,
    persistedOverlayId,
    errorVisible: afterMove.errorVisible,
    screenshot: screenshotPath,
  };
  console.log(JSON.stringify(report, null, 2));
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
