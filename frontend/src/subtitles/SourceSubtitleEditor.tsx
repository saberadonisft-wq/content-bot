import { useState } from 'react';
import { LoaderCircle, Plus, Redo2, Save, Undo2 } from 'lucide-react';
import { VirtualSubtitleList } from './VirtualSubtitleList';
import type { useSourceDocument } from './source-document';
import type { FrameTiming } from './types';

export function SourceSubtitleEditor({ source, durationMs, frameTiming, onSeek, onSave, saving }: {
  source: ReturnType<typeof useSourceDocument>; durationMs: number; frameTiming: FrameTiming;
  onSeek: (ms: number) => void; onSave: () => void; saving: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!source.document) return null;
  return <section className="source-subtitle-editor" aria-label="Chỉnh sửa phụ đề nguồn">
    <header className="source-subtitle-heading">
      <div>
        <h3>Phụ đề nguồn</h3>
        <p>{source.document.language} · Bản {source.document.revision ?? 0} · {source.document.segments.length} đoạn</p>
      </div>
      <span className="source-subtitle-hint">Chỉnh trước khi dịch</span>
    </header>
    <div className="source-subtitle-toolbar">
      <div className="source-subtitle-history" role="group" aria-label="Thao tác với phụ đề nguồn">
        <button type="button" className="studio-icon-button" disabled={!source.past.length}
          aria-label="Hoàn tác nguồn" title="Hoàn tác nguồn"
          onClick={() => source.dispatch({ type: 'undo' })}><Undo2 size={16} /></button>
        <button type="button" className="studio-icon-button" disabled={!source.future.length}
          aria-label="Làm lại nguồn" title="Làm lại nguồn"
          onClick={() => source.dispatch({ type: 'redo' })}><Redo2 size={16} /></button>
        <button type="button" className="studio-icon-button"
          aria-label="Thêm đoạn nguồn" title="Thêm đoạn nguồn"
          onClick={() => source.dispatch({ type: 'add', durationMs })}><Plus size={16} /></button>
      </div>
      <button type="button" className="studio-primary-button source-subtitle-save"
        disabled={saving} aria-busy={saving} onClick={onSave}>
        {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
        {saving ? 'Đang lưu…' : 'Lưu nguồn'}
      </button>
    </div>
    <VirtualSubtitleList title="Phụ đề nguồn" compact hideHeading respectLocks cues={source.document.segments}
      selectedCueId={selected} durationMs={durationMs} frameTiming={frameTiming}
      onSelect={setSelected} onSeek={onSeek}
      onAdd={() => source.dispatch({ type: 'add', durationMs })}
      onDelete={id => source.dispatch({ type: 'delete', id })}
      onSplit={id => source.dispatch({ type: 'split', id })}
      onMergeNext={id => source.dispatch({ type: 'merge', id })}
      onToggleLock={id => source.dispatch({ type: 'lock', id })}
      onChange={(id, patch) => source.dispatch({ type: 'edit', id, patch, durationMs })} />
  </section>;
}
