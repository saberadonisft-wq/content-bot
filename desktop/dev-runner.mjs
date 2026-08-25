import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(desktopRoot, "..");
const frontendRoot = path.join(projectRoot, "frontend");
const ownedProcesses = new Set();
const watchers = [];
let electronProcess = null;
let shuttingDown = false;
let electronRestarting = false;

const frontendRequire = createRequire(path.join(frontendRoot, "package.json"));
const electronExecutable = frontendRequire("electron");

function portOpen(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const finish = (value) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(500);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

async function waitForPort(port, label, timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await portOpen(port)) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`${label} did not start on port ${port}.`);
}

function run(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: projectRoot,
    env: process.env,
    stdio: "inherit",
    windowsHide: true,
    ...options,
  });
  ownedProcesses.add(child);
  child.once("exit", () => ownedProcesses.delete(child));
  return child;
}

function pythonPath(folder) {
  return process.platform === "win32"
    ? path.join(projectRoot, folder, ".venv", "Scripts", "python.exe")
    : path.join(projectRoot, folder, ".venv", "bin", "python");
}

async function ensureService({ port, label, command, args, cwd, watchRoot }) {
  if (await portOpen(port)) {
    console.log(`[desktop] Reusing ${label} on port ${port}.`);
    return;
  }
  let child = run(command, args, { cwd });
  await waitForPort(port, label);
  console.log(`[desktop] ${label} is ready on port ${port}.`);
  let timer = null;
  const watcher = fs.watch(watchRoot, { recursive: true }, (_event, filename) => {
    if (!filename || !filename.endsWith(".py") || filename.includes("__pycache__")) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      console.log(`[desktop] Restarting ${label} after ${filename} changed.`);
      child.kill();
      await new Promise((resolve) => child.once("exit", resolve));
      child = run(command, args, { cwd });
      await waitForPort(port, label).catch((error) => console.error(error.message));
    }, 350);
  });
  watchers.push(watcher);
}

function startElectron() {
  const electronEnv = {
    ...process.env,
    CONTENT_BOT_DESKTOP_DEV_URL: "http://127.0.0.1:5173/?view=library&tab=live",
  };
  // Some coding shells export this flag so Electron behaves like plain Node.
  // The desktop child must always run in browser mode.
  delete electronEnv.ELECTRON_RUN_AS_NODE;
  electronProcess = run(electronExecutable, [path.join(desktopRoot, "main.cjs")], {
    env: electronEnv,
    windowsHide: false,
  });
  electronProcess.once("exit", (code) => {
    if (shuttingDown) return;
    if (electronRestarting) {
      electronRestarting = false;
      startElectron();
      return;
    }
    void shutdown(code ?? 0);
  });
}

function watchDesktop() {
  let timer = null;
  const watcher = fs.watch(desktopRoot, { recursive: false }, (_event, filename) => {
    if (!filename || !filename.endsWith(".cjs") || filename.endsWith(".test.cjs")) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!electronProcess || electronProcess.killed) return;
      console.log(`[desktop] Restarting Electron after ${filename} changed.`);
      electronRestarting = true;
      electronProcess.kill();
    }, 250);
  });
  watchers.push(watcher);
}

async function shutdown(exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const watcher of watchers) watcher.close();
  for (const child of [...ownedProcesses]) {
    if (!child.killed) child.kill();
  }
  setTimeout(() => process.exit(exitCode), 300).unref();
}

process.on("SIGINT", () => void shutdown(0));
process.on("SIGTERM", () => void shutdown(0));

try {
  await ensureService({
    port: 8080,
    label: "Auth Server",
    command: pythonPath("auth-server"),
    args: ["-u", "-m", "uvicorn", "app.main:app", "--app-dir", "auth-server", "--host", "127.0.0.1", "--port", "8080"],
    cwd: projectRoot,
    watchRoot: path.join(projectRoot, "auth-server", "app"),
  });
  await ensureService({
    port: 8000,
    label: "Backend",
    command: pythonPath("backend"),
    args: ["-u", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000"],
    cwd: projectRoot,
    watchRoot: path.join(projectRoot, "backend", "app"),
  });
  if (!(await portOpen(5173))) {
    const viteCli = path.join(frontendRoot, "node_modules", "vite", "bin", "vite.js");
    run(process.execPath, [viteCli, "--host", "127.0.0.1", "--port", "5173", "--strictPort"], {
      cwd: frontendRoot,
    });
    await waitForPort(5173, "Frontend");
  } else {
    console.log("[desktop] Reusing Frontend on port 5173.");
  }
  watchDesktop();
  startElectron();
} catch (error) {
  console.error(`[desktop] ${error instanceof Error ? error.message : String(error)}`);
  await shutdown(1);
}
