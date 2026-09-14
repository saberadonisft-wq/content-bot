const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { pickProjectVideo, pickerRequest } = require("./video-picker.cjs");

test("picker rejects remote destinations before reading or uploading files", () => {
  for (const apiBase of ["https://example.com/api/v1", "http://127.0.0.1:8000/api/v1?next=x", "http://user:pass@127.0.0.1:8000/api/v1"]) {
    assert.throws(() => pickerRequest({ apiBase }));
  }
  assert.throws(() => pickerRequest({ apiBase: "http://127.0.0.1:8000/api/v1", accessToken: "bad\r\nheader" }));
});

test("picker opens configured project folder and reuses existing video without upload", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "project-video-picker-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const directory = path.join(root, "custom-data", "videos", "upload");
  await fs.mkdir(directory, { recursive: true });
  const id = "a".repeat(32);
  const filename = path.join(directory, id + ".mp4");
  await fs.writeFile(filename, "video");
  const calls = [];
  const selected = await pickProjectVideo({ apiBase: "http://127.0.0.1:8000/api/v1", accessToken: "fixture" }, {
    showOpenDialog: async options => {
      assert.equal(options.defaultPath, directory);
      assert.deepEqual(options.properties, ["openFile"]);
      return { canceled: false, filePaths: [filename] };
    },
    fetch: async (url, init) => {
      calls.push(url);
      assert.equal(init.headers.Authorization, "Bearer fixture");
      assert.equal(init.redirect, "error");
      assert.equal(init.method, undefined);
      return Response.json(url.endsWith("/directory") ? { path: directory } : { duration_ms: 1234 });
    },
  });
  assert.equal(selected.upload.video_id, id);
  assert.equal(selected.upload.media.duration_ms, 1234);
  assert.equal(selected.size, 5);
  assert.equal(calls.length, 2);
});

test("canceling the picker does not import anything", async () => {
  let calls = 0;
  const result = await pickProjectVideo({ apiBase: "http://127.0.0.1:8000/api/v1" }, {
    fetch: async () => { calls++; return Response.json({ path: os.tmpdir() }); },
    showOpenDialog: async () => ({ canceled: true, filePaths: [] }),
  });
  assert.equal(result, null);
  assert.equal(calls, 1);
});

test("video selected outside project folder is uploaded through existing API", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "project-video-import-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const directory = path.join(root, "upload");
  const filename = path.join(root, "other.webm");
  await fs.writeFile(filename, "external video");
  const result = await pickProjectVideo({ apiBase: "http://127.0.0.1:8000/api/v1" }, {
    showOpenDialog: async () => ({ canceled: false, filePaths: [filename] }),
    fetch: async (url, init) => {
      if (url.endsWith("/directory")) return Response.json({ path: directory });
      assert.ok(url.endsWith("/subtitles/upload"));
      assert.equal(init.method, "POST");
      const file = init.body.get("file");
      assert.equal(file.name, "other.webm");
      assert.equal(file.type, "video/webm");
      assert.equal(await file.text(), "external video");
      return Response.json({ video_id: "b".repeat(32), media: { duration_ms: 2000 } });
    },
  });
  assert.equal(result.name, "other.webm");
  assert.equal(result.upload.video_id, "b".repeat(32));
});
