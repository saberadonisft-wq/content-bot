const fs = require("node:fs");
const path = require("node:path");

function pickerRequest(payload) {
  const base = new URL(String(payload?.apiBase || ""));
  if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || base.port !== "8000" ||
      base.pathname.replace(/\/$/, "") !== "/api/v1" || base.username || base.password || base.search || base.hash) {
    throw new Error("Địa chỉ backend chọn video không hợp lệ.");
  }
  const token = String(payload?.accessToken || "");
  if (token.length > 16_384 || /[\r\n]/.test(token)) throw new Error("Phiên đăng nhập không hợp lệ.");
  return { base: base.toString().replace(/\/$/, ""), headers: token ? { Authorization: `Bearer ${token}` } : {} };
}

async function pickProjectVideo(payload, { showOpenDialog, fetch = globalThis.fetch }) {
  const request = pickerRequest(payload);
  const read = async (url, init = {}) => {
    const response = await fetch(url, { headers: request.headers, redirect: "error", ...init });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Không mở được video (${response.status}).`);
    return body;
  };
  const location = await read(`${request.base}/videos/directory`);
  if (typeof location.path !== "string" || !path.isAbsolute(location.path)) throw new Error("Thư mục video dự án không hợp lệ.");
  const directory = path.resolve(location.path);
  await fs.promises.mkdir(directory, { recursive: true });
  const selection = await showOpenDialog({
    title: "Chọn video dự án",
    defaultPath: directory,
    properties: ["openFile"],
    filters: [{ name: "Video", extensions: ["mp4", "webm", "mkv"] }],
  });
  if (selection.canceled || !selection.filePaths.length) return null;
  const selected = path.resolve(selection.filePaths[0]);
  const ext = path.extname(selected).toLowerCase();
  if (![".mp4", ".webm", ".mkv"].includes(ext)) throw new Error("Hãy chọn video MP4, WebM hoặc MKV.");
  const stat = await fs.promises.stat(selected);
  if (!stat.isFile() || !stat.size) throw new Error("File video không hợp lệ hoặc trống.");
  const name = path.basename(selected);
  const id = path.basename(selected, path.extname(selected));
  let upload;
  if (path.dirname(selected) === directory && /^[a-f0-9]{12,32}$/.test(id)) {
    // Already in the project: reuse it instead of copying/uploading the entire video.
    const media = await read(`${request.base}/subtitles/video/${id}/metadata`);
    upload = { video_id: id, filename: name, video_url: `/api/v1/subtitles/video/${id}`, media };
  } else {
    // Stream another file selected by the user; never shuttle large buffers through IPC.
    const type = ext === ".webm" ? "video/webm" : ext === ".mkv" ? "video/x-matroska" : "video/mp4";
    const form = new FormData();
    form.append("file", await fs.openAsBlob(selected, { type }), name);
    upload = await read(`${request.base}/subtitles/upload`, { method: "POST", body: form });
  }
  return { name, size: stat.size, lastModified: stat.mtimeMs, upload };
}

module.exports = { pickProjectVideo, pickerRequest };
