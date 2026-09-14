import { useEffect, useState } from "react";
import { api } from "./api";
import type { GeminiKey, GeminiKeyList } from "./api/settings";

const stateNames: Record<string, string> = { untested: "Chưa kiểm tra", ready: "Sẵn sàng", quota_wait: "Chờ hạn mức", permission_error: "Lỗi xác thực/quyền", disabled: "Đã tắt", busy: "Đang xử lý" };
const inputClass = "w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white";

function KeyRow({ row, busy, run }: { row: GeminiKey; busy: boolean; run: (work: () => Promise<GeminiKeyList>) => Promise<void> }) {
  const [name, setName] = useState(row.name);
  const [model, setModel] = useState("gemini-3.6-flash");
  return <li className="space-y-2 rounded-lg border border-slate-700 p-3">
    <div className="flex justify-between gap-2 text-xs"><span className="font-mono">{row.masked_key}</span><span>{stateNames[row.state] ?? row.state}</span></div>
    <div className="grid gap-2 sm:grid-cols-2">
      <label className="text-xs">Tên key<input className={inputClass} value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
    </div>
    <label className="block text-xs">Model kiểm tra<input className={inputClass} value={model} onChange={e => setModel(e.target.value)} /></label>
    <div className="flex flex-wrap gap-3 text-sm">
      <button type="button" disabled={busy || !name.trim()} onClick={() => void run(() => api.updateGeminiKey(row.id, { name: name.trim() }))}>Lưu tên</button>
      <button type="button" disabled={busy} onClick={() => void run(() => api.updateGeminiKey(row.id, { enabled: !row.enabled }))}>{row.enabled ? "Tắt" : "Bật"}</button>
      <button type="button" disabled={busy || !model.trim()} onClick={() => void run(() => api.checkGeminiKey(row.id, model.trim()))}>Kiểm tra</button>
      <button type="button" className="text-rose-300" disabled={busy} onClick={() => void run(() => api.deleteGeminiKey(row.id))}>Xóa</button>
    </div>
    {row.checked_at && <p className="text-xs text-slate-400">Đã kiểm tra quyền đọc thông tin {row.checked_model} lúc {new Date(row.checked_at).toLocaleString()}. Không bảo đảm quota sinh nội dung.</p>}
  </li>;
}

export function GeminiKeySettings({ onChanged }: { onChanged: () => Promise<void> }) {
  const [data, setData] = useState<GeminiKeyList | null>(null);
  const [keys, setKeys] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api.getGeminiKeys(controller.signal).then(setData).catch(err => { if (!controller.signal.aborted) setMessage(String(err)); });
    return () => controller.abort();
  }, []);
  async function run(work: () => Promise<GeminiKeyList>) {
    setBusy(true); setMessage("");
    try {
      const next = await work(); setData(next);
      setMessage(next.added || next.duplicates ? `Đã thêm ${next.added} key; bỏ qua ${next.duplicates} key trùng.` : "Đã cập nhật.");
      await onChanged();
    } catch (err) { setMessage(err instanceof Error ? err.message : "Không thể cập nhật key."); }
    finally { setBusy(false); }
  }
  const filtered = (data?.keys ?? []).filter(k => `${k.name} ${k.masked_key}`.toLowerCase().includes(search.toLowerCase()));
  const lastPage = Math.max(0, Math.ceil(filtered.length / 10) - 1);
  const currentPage = Math.min(page, lastPage);
  return <div className="space-y-3 text-slate-300">
    <h4 className="font-semibold text-white">Gemini AI — Danh sách API key</h4>
    <p className="text-xs text-slate-400">Key được mã hóa trên máy và gửi tới Google khi gọi API. Tất cả key đang bật được sử dụng, mỗi key xử lý một đoạn đồng thời.</p>
    <label className="block text-sm">Thêm key, mỗi dòng một key<textarea className={inputClass} rows={3} autoComplete="off" spellCheck={false} value={keys} onChange={e => setKeys(e.target.value)} /></label>
    <button type="button" disabled={busy || !keys.trim()} onClick={() => void run(async () => { const result = await api.importGeminiKeys(keys); setKeys(""); return result; })}>Thêm vào Vault</button>
    <p role="status" className="text-xs">{message}</p>
    <label className="block text-xs">Tìm key<input className={inputClass} value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} /></label>
    <ul className="space-y-3">{filtered.slice(currentPage * 10, currentPage * 10 + 10).map(row => <KeyRow key={row.id} row={row} busy={busy} run={run} />)}</ul>
    <div className="flex items-center justify-between text-xs">
      <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Trước</button>
      <span>{filtered.length} key · Trang {currentPage + 1}/{lastPage + 1}</span>
      <button type="button" disabled={currentPage >= lastPage} onClick={() => setPage(currentPage + 1)}>Sau</button>
    </div>
  </div>;
}
