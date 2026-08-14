import { useEffect, useRef } from "react";
import type JASSUB from "jassub";
import modernWasmUrl from "jassub/dist/wasm/jassub-worker-modern.wasm?url";
import wasmUrl from "jassub/dist/wasm/jassub-worker.wasm?url";
import workerUrl from "jassub/dist/wasm/jassub-worker.js?url";

type LibassPreviewProps = {
  video: HTMLVideoElement | null;
  host: HTMLDivElement | null;
  assContent: string | null;
  enabled: boolean;
  onReadyChange: (ready: boolean) => void;
};

const ARIMO_REGULAR_URL = "/fonts/arimo/Arimo-Variable.ttf";
const ARIMO_ITALIC_URL = "/fonts/arimo/Arimo-Italic-Variable.ttf";
const EMPTY_ASS_TRACK = `[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arimo,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
`;

export function LibassPreview({
  video,
  host,
  assContent,
  enabled,
  onReadyChange,
}: LibassPreviewProps) {
  const rendererRef = useRef<JASSUB | null>(null);
  const latestTrackRef = useRef(assContent);

  useEffect(() => {
    latestTrackRef.current = assContent;
    const renderer = rendererRef.current;
    if (!renderer || !assContent) return;
    void renderer.ready
      .then(async () => {
        await renderer.renderer.setTrack(assContent);
        await renderer.resize(true);
        onReadyChange(true);
      })
      .catch((error: unknown) => {
        console.warn("Không thể cập nhật track libass preview", error);
        onReadyChange(false);
      });
  }, [assContent, onReadyChange]);

  useEffect(() => {
    if (!enabled || !video || !host) return undefined;

    let disposed = false;
    let renderer: JASSUB | null = null;

    void import("jassub")
      .then(async ({ default: JASSUBRenderer }) => {
        if (disposed) return;
        const canvas = document.createElement("canvas");
        canvas.className = "JASSUB";
        canvas.style.position = "absolute";
        canvas.style.pointerEvents = "none";
        host.append(canvas);
        renderer = new JASSUBRenderer({
          video,
          canvas,
          subContent: latestTrackRef.current ?? EMPTY_ASS_TRACK,
          workerUrl,
          wasmUrl,
          modernWasmUrl,
          fonts: [ARIMO_REGULAR_URL, ARIMO_ITALIC_URL],
          availableFonts: {
            Arimo: ARIMO_REGULAR_URL,
          },
          defaultFont: "Arimo",
          queryFonts: "local",
        });
        rendererRef.current = renderer;
        await renderer.ready;
        if (disposed) return;
        const latestTrack = latestTrackRef.current;
        if (latestTrack) {
          await renderer.renderer.setTrack(latestTrack);
        }
        await renderer.resize(true);
        onReadyChange(true);
      })
      .catch((error: unknown) => {
        console.warn("Không thể khởi tạo libass preview", error);
        onReadyChange(false);
      });

    return () => {
      disposed = true;
      onReadyChange(false);
      if (rendererRef.current === renderer) rendererRef.current = null;
      if (renderer) void renderer.destroy().catch(() => undefined);
    };
  }, [enabled, host, onReadyChange, video]);

  return null;
}
