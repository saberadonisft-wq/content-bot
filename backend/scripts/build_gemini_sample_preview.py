"""Build an offline video + subtitle comparison for manual sample review."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "gemini-pipeline"


def main():
    data = {name: json.loads((ROOT / name / "result.json").read_text(encoding="utf-8")) for name in ("baseline", "new-sample")}
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = r'''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Duyệt mẫu phụ đề Gemini</title>
<style>body{max-width:1100px;margin:24px auto;padding:0 18px;background:#10151b;color:#e5e9ee;font:16px/1.55 system-ui}h1{font-size:26px}video{width:100%;max-height:440px;background:#000}button,select{font:inherit;background:#243040;color:#fff;border:1px solid #607287;padding:6px 12px;border-radius:5px;cursor:pointer}button:focus-visible,select:focus-visible{outline:3px solid #80c7ff}.columns{display:grid;grid-template-columns:1fr 1fr;gap:20px}.current{min-height:90px;padding:12px;border-left:3px solid #7ac2ed;background:#1a2431;white-space:pre-wrap}.source{color:#bac6d4;font-size:14px}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #34404e;padding:8px}small{color:#bdc9d6}.review{color:#f4cc82}.scroll{max-height:430px;overflow:auto}@media(max-width:650px){.columns{grid-template-columns:1fr}}</style>
<h1>Duyệt ba phút đầu · Gemini Subtitle Pipeline</h1>
<p>Chưa có phụ đề chuẩn. So sánh lời dịch với audio và chữ gốc trên hình; kiểm tra câu thiếu, trùng và timing. Số cue nhiều hơn không chứng minh chất lượng tốt hơn.</p>
<video id="video" controls preload="metadata" src="new-sample/sample.mp4"></video>
<div class="columns"><section><h2>Baseline</h2><div id="base" class="current"></div><small id="baseinfo"></small></section><section><h2>Pipeline mới</h2><div id="new" class="current"></div><small id="newinfo"></small></section></div>
<p><label for="version">Danh sách cue: </label><select id="version"><option value="new-sample">Pipeline mới</option><option value="baseline">Baseline</option></select> <a href="new-sample/subtitles.srt">Tải SRT mới</a> · <a href="baseline/subtitles.srt">Tải SRT baseline</a></p>
<div class="scroll"><table><thead><tr><th>Thời gian</th><th>Bản dịch và lời gốc</th><th>Ghi chú</th></tr></thead><tbody id="rows"></tbody></table></div>
<p><small>Bản mới dùng chia đoạn theo khoảng nghỉ và đối chiếu vùng nối. Chưa phải bản nghiệm thu toàn video, chưa chạy bước căn audio tùy chọn hoặc lượt kiểm tra/sửa bằng AI.</small></p>
<script>const data=__DATA__;const video=document.getElementById('video');
function time(ms){return (ms/1000).toFixed(2)+'s'}
function display(id,name){const segments=data[name].document.segments;const active=segments.filter(c=>c.start_ms<=video.currentTime*1000&&c.end_ms>video.currentTime*1000);document.getElementById(id).textContent=active.map(c=>c.text+'\n'+(c.source_text||c.secondary_text||'')).join('\n\n')||'—';document.getElementById(id+'info').textContent=segments.length+' cue · '+(data[name].processing_seconds||'—')+'s xử lý · Model '+data[name].model}
video.addEventListener('timeupdate',()=>{display('base','baseline');display('new','new-sample')});
function render(){const name=document.getElementById('version').value;const body=document.getElementById('rows');body.replaceChildren();for(const cue of data[name].document.segments){const tr=document.createElement('tr');const seek=document.createElement('button');seek.textContent=time(cue.start_ms)+' – '+time(cue.end_ms);seek.onclick=()=>{video.currentTime=cue.start_ms/1000;video.play().catch(()=>{})};const td=document.createElement('td');td.append(seek);const text=document.createElement('td');const translation=document.createElement('div');translation.textContent=cue.text;const source=document.createElement('div');source.className='source';source.textContent=cue.source_text||cue.secondary_text||'';text.append(translation,source);const status=document.createElement('td');status.textContent=cue.needs_review?'Cần duyệt':'';status.className='review';tr.append(td,text,status);body.append(tr)}}
document.getElementById('version').onchange=render;render();display('base','baseline');display('new','new-sample');</script></html>'''
    (ROOT / "preview.html").write_text(template.replace("__DATA__", encoded), encoding="utf-8")
    report = {name: {"cue_count": result["segment_count"], "processing_seconds": result["processing_seconds"],
                    "warning_count": len(result["warnings"]), "needs_review_count": sum(c.get("needs_review", False) for c in result["document"]["segments"]),
                    "chunk_count": result["chunk_count"], "model": result["model"]} for name, result in data.items()}
    (ROOT / "sample-comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
