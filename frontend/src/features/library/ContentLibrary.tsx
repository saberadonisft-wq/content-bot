import { Sources } from "../sources/Sources";
import { RegisteredChannels } from "./RegisteredChannels";

import {
LoaderCircle
} from "lucide-react";
import { lazy,Suspense } from "react";
import {
Batch,
Keyword,
Source
} from "../../api";
import { LiveChannelWall } from "../../LiveChannelWall";

import { LIBRARY_TABS,LibraryTab } from "../shared/presentation";

const VideoLibrary = lazy(() => import("../../VideoLibrary").then(module => ({ default: module.VideoLibrary })));
export function ContentLibrary({
  tab,
  sources,
  loading,
  selected,
  batch,
  running,
  pendingSourceId,
  pendingChannelId,
  canceling,
  onRunSource,
  onRunChannel,
  onRunChannels,
  onConnectionsChanged,
  onCancel,
}: {
  tab: LibraryTab;
  sources: Source[];
  loading: boolean;
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingSourceId: string | null;
  pendingChannelId: string | null;
  canceling: boolean;
  onRunSource: (sourceId: string) => void;
  onRunChannel: (channelId: string) => void;
  onRunChannels: (channelIds: string[]) => void;
  onConnectionsChanged: () => Promise<void>;
  onCancel: () => void;
}) {
  return (
    <>
      <section className="page-head content-library-head">
        <div>
          <p className="eyebrow">Kho nội dung</p>
          <h1>Nguồn &amp; Video</h1>
          <p className="subtle">
            Quản lý kênh thu thập, kết nối nền tảng và toàn bộ video trong cùng một nơi.
          </p>
        </div>
        {selected && <span className="library-topic-context">Chủ đề: {selected.name}</span>}
      </section>
      <section
        id={`library-panel-${tab}`}
        className="content-library-panel"
        aria-label={LIBRARY_TABS.find((item) => item.id === tab)?.label}
      >
        {tab === "channels" && (
          <RegisteredChannels
            key={selected?.id ?? "none"}
            selected={selected}
            batch={batch}
            running={running}
            pendingChannelId={pendingChannelId}
            onRunChannel={onRunChannel}
            onRunChannels={onRunChannels}
          />
        )}
        {tab === "live" && (
          <LiveChannelWall
            key={selected?.id ?? "none"}
            selected={selected}
            sources={sources}
            onChanged={onConnectionsChanged}
          />
        )}
        {tab === "connections" && (
          <Sources
            embedded
            sources={sources}
            loading={loading}
            selected={selected}
            batch={batch}
            running={running}
            pendingSourceId={pendingSourceId}
            canceling={canceling}
            onRunSource={onRunSource}
            onConnectionsChanged={onConnectionsChanged}
            onCancel={onCancel}
          />
        )}
        {tab === "videos" && (
          <Suspense fallback={<div className="library-loading"><LoaderCircle className="spin" size={24} /> Đang tải thư viện video…</div>}>
            <VideoLibrary embedded />
          </Suspense>
        )}
      </section>
    </>
  );
}

