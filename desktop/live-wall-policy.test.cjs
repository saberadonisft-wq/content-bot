const assert = require("node:assert/strict");
const test = require("node:test");

const {
  clippedViewBounds,
  normalizeBounds,
  normalizeSyncPayload,
  safeAuthUrl,
  safeChannelUrl,
  safeSubtitleDownload,
} = require("./live-wall-policy.cjs");

test("safeChannelUrl accepts configured platform hosts only", () => {
  assert.equal(safeChannelUrl("x", "https://x.com/NTE_Ani_Info"), "https://x.com/NTE_Ani_Info");
  assert.equal(safeChannelUrl("x", "https://mobile.x.com/NTE_Ani_Info"), "https://mobile.x.com/NTE_Ani_Info");
  assert.equal(safeChannelUrl("x", "https://x.com.evil.test/NTE_Ani_Info"), null);
  assert.equal(safeChannelUrl("web", "https://example.test/feed"), null);
  assert.equal(safeChannelUrl("x", "javascript:alert(1)"), null);
});

test("safeAuthUrl recognizes blocked X Google login without accepting arbitrary hosts", () => {
  assert.equal(
    safeAuthUrl("x", "https://accounts.google.com/o/oauth2/v2/auth?client_id=test"),
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=test",
  );
  assert.equal(safeAuthUrl("x", "https://x.com/i/flow/login"), "https://x.com/i/flow/login");
  assert.equal(safeAuthUrl("x", "https://accounts.google.com.evil.test/login"), null);
  assert.equal(safeAuthUrl("reddit", "https://accounts.google.com/login"), null);
  assert.equal(safeAuthUrl("x", "javascript:alert(1)"), null);
});

test("safeSubtitleDownload only accepts rendered MP4 files from the local backend", () => {
  const filename = "subtitled_123456789abc_abcdef123456.mp4";
  assert.deepEqual(
    safeSubtitleDownload(
      `http://127.0.0.1:8000/api/v1/subtitles/renders/${filename}?t=123`,
      filename,
    ),
    {
      url: `http://127.0.0.1:8000/api/v1/subtitles/renders/${filename}?t=123`,
      filename,
    },
  );
  assert.equal(
    safeSubtitleDownload(`https://example.com/${filename}`, filename),
    null,
  );
  assert.equal(
    safeSubtitleDownload("http://127.0.0.1:8000/api/v1/health", filename),
    null,
  );
});

test("normalizeBounds rejects invisible or unreasonable rectangles", () => {
  assert.deepEqual(normalizeBounds({ x: 10.4, y: 20.6, width: 640, height: 480 }), {
    x: 10,
    y: 21,
    width: 640,
    height: 480,
  });
  assert.equal(normalizeBounds({ x: 0, y: 0, width: 40, height: 40 }), null);
  assert.deepEqual(normalizeBounds({ x: 0, y: -120, width: 640, height: 480 }), {
    x: 0,
    y: -120,
    width: 640,
    height: 480,
  });
  assert.equal(normalizeBounds({ x: -20_001, y: 0, width: 640, height: 480 }), null);
});

test("clippedViewBounds clips presentation without resizing the web viewport", () => {
  assert.deepEqual(
    clippedViewBounds(
      { x: 40, y: -180, width: 400, height: 600 },
      { width: 1200, height: 800 },
    ),
    {
      container: { x: 40, y: 0, width: 400, height: 420 },
      content: { x: 0, y: -180, width: 400, height: 600 },
    },
  );
  assert.equal(
    clippedViewBounds(
      { x: 40, y: 900, width: 400, height: 600 },
      { width: 1200, height: 800 },
    ),
    null,
  );
});

test("normalizeSyncPayload caps views, preserves order and rejects duplicates", () => {
  const payload = normalizeSyncPayload({
    ownerId: "user-a",
    keywordId: 3,
    views: [
      {
        channelId: "channel-2",
        sourceId: "x",
        url: "https://x.com/channel_2",
        bounds: { x: 0, y: 100, width: 640, height: 480 },
        visible: true,
      },
      {
        channelId: "channel-1",
        sourceId: "reddit",
        url: "https://www.reddit.com/r/gaming/",
        bounds: { x: 650, y: 100, width: 640, height: 480 },
        visible: false,
      },
    ],
  });
  assert.deepEqual(payload.views.map((view) => view.channelId), ["channel-2", "channel-1"]);
  assert.throws(() =>
    normalizeSyncPayload({
      ownerId: "user-a",
      keywordId: 3,
      views: [payload.views[0], payload.views[0]],
    }),
  );
});
