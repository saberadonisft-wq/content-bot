import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const edgePath = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const debuggingPort = 9333;
const profileDir = await mkdtemp(path.join(tmpdir(), "content-bot-settings-audit-"));
const artifactDir = path.resolve("..", "artifacts");
const browser = spawn(
  edgePath,
  [
    "--headless=new",
    `--remote-debugging-port=${debuggingPort}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--disable-default-apps",
    "--disable-extensions",
    "--window-size=1440,900",
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true },
);

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function fetchJson(url, attempts = 40) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
    } catch {
      // Edge is still starting.
    }
    await delay(100);
  }
  throw new Error(`Chrome DevTools endpoint did not become ready: ${url}`);
}

function connectCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  let nextId = 1;
  const pending = new Map();

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id) return;
    const callback = pending.get(message.id);
    if (!callback) return;
    pending.delete(message.id);
    if (message.error) callback.reject(new Error(message.error.message));
    else callback.resolve(message.result);
  });

  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  return {
    ready,
    send(method, params = {}) {
      const id = nextId;
      nextId += 1;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      socket.close();
    },
  };
}

async function capture(client, fileName) {
  const result = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await writeFile(path.join(artifactDir, fileName), Buffer.from(result.data, "base64"));
}

try {
  await mkdir(artifactDir, { recursive: true });
  const pages = await fetchJson(`http://127.0.0.1:${debuggingPort}/json/list`);
  const page = pages.find((item) => item.type === "page");
  if (!page) throw new Error("No debuggable page was created.");

  const client = connectCdp(page.webSocketDebuggerUrl);
  await client.ready;
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `(() => {
      const user = {
        id: 'settings-audit',
        email: 'audit@example.test',
        display_name: 'Settings Audit',
        role: 'user',
        status: 'approved',
        auth_provider: 'email',
        created_at: new Date().toISOString(),
      };
      const configuredKeys = {
        youtube_api_key: false,
        x_bearer_token: false,
        reddit_client_id: false,
        reddit_client_secret: false,
        reddit_user_agent: false,
        meta_access_token: false,
        instagram_professional_user_id: false,
        facebook_page_access_token: false,
        facebook_page_id: false,
        facebook_page_username: false,
        tiktok_client_key: false,
        tiktok_client_secret: false,
        tiktok_redirect_uri: false,
        gemini_api_key: true,
      };
      const credentialStatus = {
        is_master_password_set: true,
        is_unlocked: true,
        configured_keys: configuredKeys,
        vault_configured_keys: configuredKeys,
        env_configured_keys: Object.fromEntries(Object.keys(configuredKeys).map((key) => [key, false])),
        credential_sources: Object.fromEntries(Object.keys(configuredKeys).map((key) => [key, key === 'gemini_api_key' ? 'vault' : 'none'])),
        masked_keys: { gemini_api_key: 'AIza••••••••••••••••••••••••••••••••' },
        platforms: {
          youtube: false,
          x_twitter: false,
          reddit: false,
          meta_instagram: false,
          facebook_page: false,
          tiktok: false,
          gemini: true,
        },
      };
      localStorage.setItem('content_bot_access_token', 'settings-audit-token');
      localStorage.setItem('content_bot_user', JSON.stringify(user));
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const url = String(typeof input === 'string' ? input : input.url);
        if (url.endsWith('/auth/me')) {
          return Promise.resolve(new Response(JSON.stringify(user), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }));
        }
        if (url.includes('/api/v1/credentials/status')) {
          return Promise.resolve(new Response(JSON.stringify(credentialStatus), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }));
        }
        return nativeFetch(input, init);
      };
    })();`,
  });
  await client.send("Page.navigate", { url: "http://127.0.0.1:5173/" });
  await delay(1800);

  const clickResult = await client.send("Runtime.evaluate", {
    expression: `(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent?.includes('Cài đặt API'));
      if (!button) return { clicked: false, text: document.body.innerText.slice(0, 500) };
      button.click();
      return { clicked: true };
    })()`,
    returnByValue: true,
  });
  if (!clickResult.result.value?.clicked) {
    throw new Error(`Settings button not found. Page text: ${clickResult.result.value?.text ?? ""}`);
  }

  await delay(900);
  await capture(client, "settings-modal-desktop.png");
  await client.send("Runtime.evaluate", {
    expression: `(() => {
      const tab = [...document.querySelectorAll('.cb-settings-tab')]
        .find((item) => item.textContent?.includes('Gemini AI'));
      tab?.click();
      return Boolean(tab);
    })()`,
    returnByValue: true,
  });
  await delay(250);
  await capture(client, "settings-modal-gemini.png");
  client.close();
  process.stdout.write(`${path.join(artifactDir, "settings-modal-desktop.png")}\n`);
  process.stdout.write(`${path.join(artifactDir, "settings-modal-gemini.png")}\n`);
} finally {
  browser.kill();
  await rm(profileDir, { recursive: true, force: true, maxRetries: 4, retryDelay: 100 });
}
