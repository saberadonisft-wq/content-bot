import { describe, expect, it } from "vitest";

import type { ChannelSubscription, Source } from "./api";
import {
  channelSupportsEmbed,
  initialLiveWallChannelIds,
  liveWallPage,
  tiktokCreatorUsername,
  xProfileUsername,
} from "./liveWallModel";

describe("initialLiveWallChannelIds", () => {
  const channels = [
    { id: "channel-1" },
    { id: "channel-2" },
    { id: "channel-3" },
  ] as ChannelSubscription[];

  it("shows all saved channels when an older topic has no Live Wall selection", () => {
    expect(initialLiveWallChannelIds(channels, [])).toEqual([
      "channel-1",
      "channel-2",
      "channel-3",
    ]);
  });

  it("preserves a valid configured order and removes duplicates", () => {
    expect(
      initialLiveWallChannelIds(channels, [
        "channel-3",
        "missing",
        "channel-1",
        "channel-3",
      ]),
    ).toEqual(["channel-3", "channel-1"]);
  });
});

describe("liveWallPage", () => {
  it("paginates seven channels into 4 + 3 while preserving order", () => {
    const channels = Array.from({ length: 7 }, (_, index) => `channel-${index}`);
    expect(liveWallPage(channels, 4, 0).channels).toEqual(channels.slice(0, 4));
    expect(liveWallPage(channels, 4, 1)).toEqual({
      pageCount: 2,
      safePage: 1,
      channels: channels.slice(4),
    });
  });

  it("clamps a stale page after the selection shrinks", () => {
    expect(liveWallPage(["only"], 6, 9)).toEqual({
      pageCount: 1,
      safePage: 0,
      channels: ["only"],
    });
  });
});

describe("TikTok creator validation", () => {
  it("accepts a creator profile and rejects content or lookalike hosts", () => {
    expect(tiktokCreatorUsername("https://www.tiktok.com/@game.dev")).toBe("game.dev");
    expect(
      tiktokCreatorUsername("https://www.tiktok.com/@game.dev/video/123456"),
    ).toBeNull();
    expect(tiktokCreatorUsername("https://tiktok.com.evil.test/@game.dev")).toBeNull();
  });
});

describe("X profile validation", () => {
  it("extracts an X or Twitter profile and rejects non-profile URLs", () => {
    expect(xProfileUsername("https://x.com/NTE_Ani_Info")).toBe("NTE_Ani_Info");
    expect(xProfileUsername("https://twitter.com/Nteupdates/")).toBe("Nteupdates");
    expect(xProfileUsername("https://x.com/Nteupdates/status/123")).toBeNull();
    expect(xProfileUsername("https://x.com/explore")).toBeNull();
    expect(xProfileUsername("https://x.com.evil.test/Nteupdates")).toBeNull();
  });
});

describe("embed capability", () => {
  it("uses only an enabled render_embed operation for the matching source", () => {
    const channel = { source_id: "x" } as ChannelSubscription;
    const source = {
      id: "x",
      operations: [{ id: "render_embed", enabled: true }],
    } as Source;
    expect(channelSupportsEmbed(channel, [source])).toBe(true);
    expect(
      channelSupportsEmbed(channel, [
        { ...source, operations: [{ id: "render_embed", enabled: false }] } as Source,
      ]),
    ).toBe(false);
  });
});
