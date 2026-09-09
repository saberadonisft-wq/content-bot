import { describe, expect, it } from "vitest";

import { desktopCanRenderSource, desktopViewPlacement } from "./desktopLiveWall";

describe("desktop live wall source policy", () => {
  it("allows fixed social platforms used by saved channels", () => {
    expect(desktopCanRenderSource("x")).toBe(true);
    expect(desktopCanRenderSource("TikTok")).toBe(true);
    expect(desktopCanRenderSource("reddit")).toBe(true);
  });

  it("keeps arbitrary web and Mastodon instances on the safe fallback path", () => {
    expect(desktopCanRenderSource("web")).toBe(false);
    expect(desktopCanRenderSource("mastodon")).toBe(false);
    expect(desktopCanRenderSource("unknown")).toBe(false);
  });
});

describe("desktop live wall placement", () => {
  it("keeps the full page viewport while the host is partially scrolled offscreen", () => {
    expect(desktopViewPlacement(
      { left: 40, top: -180, right: 440, bottom: 420, width: 400, height: 600 },
      { width: 1200, height: 800 },
    )).toEqual({
      bounds: { x: 40, y: -180, width: 400, height: 600 },
      visible: true,
    });
  });

  it("keeps an offscreen host mounted but marks its native view hidden", () => {
    expect(desktopViewPlacement(
      { left: 40, top: 900, right: 440, bottom: 1500, width: 400, height: 600 },
      { width: 1200, height: 800 },
    )).toEqual({
      bounds: { x: 40, y: 900, width: 400, height: 600 },
      visible: false,
    });
  });

  it("hides a native view when renderer content obscures its host", () => {
    expect(desktopViewPlacement(
      { left: 40, top: 120, right: 440, bottom: 720, width: 400, height: 600 },
      { width: 1200, height: 800 },
      false,
    )).toEqual({
      bounds: { x: 40, y: 120, width: 400, height: 600 },
      visible: false,
    });
  });
});
