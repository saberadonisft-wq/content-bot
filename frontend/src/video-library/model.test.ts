import { describe, expect, it } from "vitest";
import { extractVideoLinks, mediaUrl, videoMatches } from "./model";
import type { VideoLibraryItem } from "../api";

describe("video link import", () => {
  it("extracts short links from Bilibili share text and keeps selected parts", () => {
    expect(extractVideoLinks("【视频】 https://b23.tv/abc。\nhttps://www.bilibili.com/video/BV1?p=2&foo=bar\nhttps://b23.tv/abc")).toEqual([
      "https://b23.tv/abc", "https://www.bilibili.com/video/BV1?p=2&foo=bar",
    ]);
  });
  it("ignores non-web links and returns no invented URLs", () => {
    expect(extractVideoLinks("file:///video.mp4 BV1abc javascript:alert(1)")).toEqual([]);
    expect(mediaUrl("javascript:alert(1)")).toBe("");
  });
  it("preserves API query parameters", () => {
    expect(mediaUrl("/api/v1/videos/abc/thumbnail?type=original")).toMatch(/\/api\/v1\/videos\/abc\/thumbnail\?type=original$/);
  });
});

describe("library filters", () => {
  const video: VideoLibraryItem = { id: "abc", title: "Video thử nghiệm", filename: "abc.mp4", type: "original", downloaded: true,
    platform: "Bilibili", source_url: "https://b23.tv/xyz", size_bytes: 1000, created_at: "2026-09-14", thumbnail_url: "", video_url: "" };
  it("distinguishes link downloads from uploads without changing their subtitle-compatible type", () => {
    expect(videoMatches(video, "", "downloaded", "Bilibili")).toBe(true);
    expect(videoMatches(video, "", "original", "")).toBe(false);
    expect(videoMatches(video, "", "all", "YouTube")).toBe(false);
  });
  it("searches titles and original links case-insensitively", () => {
    expect(videoMatches(video, "THỬ NGHIỆM", "all", "")).toBe(true);
    expect(videoMatches(video, "b23.tv/xyz", "all", "")).toBe(true);
    expect(videoMatches(video, "missing", "all", "")).toBe(false);
  });
});
