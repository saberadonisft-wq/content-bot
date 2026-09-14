import { describe, expect, it } from "vitest";
import { isLongCue, sentenceCount } from "./readability";

describe("long subtitle detection", () => {
  it.each([
    ["Tướng quân, chưa biết ư? Ngài mở tiệc. Mau đến!", 3],
    ["Xin chào. Tôi đây", 2],
    ["Đi?! Thật ư?!", 2],
    ['Anh nói: “Đi thôi!”', 1],
    ["你好。你在吗？我来了！", 3],
    ["Giá 3.14 và 1,5 đồng.", 1],
    ["TS. An ở TP.HCM hôm nay.", 1],
    ["Dr. Smith đến từ U.S.A. hôm qua.", 1],
    ["Tôi... vẫn đang nghĩ… thôi.", 1],
    ["...?!", 0],
  ])("counts sentence candidates in %s", (text, count) => {
    expect(sentenceCount(text)).toBe(count);
    expect(isLongCue({ text, start_ms: 0, end_ms: 3000 })).toBe(count > 1);
  });
  it("uses inclusive limits and flags long text, duration or too many lines", () => {
    const cue = { text: "a".repeat(84), start_ms: 1000, end_ms: 7000 };
    expect(isLongCue(cue)).toBe(false);
    expect(isLongCue({ ...cue, text: "a".repeat(85) })).toBe(true);
    expect(isLongCue({ ...cue, end_ms: 7001 })).toBe(true);
    expect(isLongCue({ ...cue, text: "Một\nHai\nBa" })).toBe(true);
  });
  it("counts Unicode characters consistently with the backend", () => {
    expect(isLongCue({ text: "ế".normalize("NFD").repeat(84), start_ms: 0, end_ms: 1000 })).toBe(false);
    expect(isLongCue({ text: "😀".repeat(84), start_ms: 0, end_ms: 1000 })).toBe(false);
  });
});
