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
const VideoAcquisition = lazy(() => import("./VideoAcquisition").then(module => ({ default: module.VideoAcquisition })));
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
          <h1>{tab === "videos" ? "Tải & quản lý video" : tab === "acquisition" ? "Cào và chọn video" : "Nguồn & Video"}</h1>
          <p className="subtle">
            {tab === "videos" ? "Dán link, tải video về máy và lưu vào thư viện của bạn." : tab === "acquisition" ? "Cào metadata trước, chọn đúng video cần tải rồi theo dõi tiến độ." : "Quản lý kênh thu thập, kết nối nền tảng và toàn bộ video trong cùng một nơi."}
          </p>
        </div>
        {selected && tab !== "videos" && tab !== "acquisition" && <span className="library-topic-context">Chủ đề: {selected.name}</span>}
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
        {tab === "acquisition" && (
          <Suspense fallback={<div className="library-loading"><LoaderCircle className="spin" size={24} /> Đang tải công cụ cào…</div>}>
            <VideoAcquisition />
          </Suspense>
        )}
      </section>
    </>
  );
}

