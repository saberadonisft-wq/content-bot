"""Offline browser regressions for source documents, extraction jobs and OCR geometry."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HTML = r"""<!doctype html><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const script = await (await fetch('/src/subtitles/useExtractionJobs.ts')).text();
const reactPath = script.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useExtractionJobs} = await import('/src/subtitles/useExtractionJobs.ts');
const {useSourceDocument} = await import('/src/subtitles/source-document.ts');
const {SubtitleOcrBox} = await import('/src/subtitles/SubtitleOcrBox.tsx');
const {useDraftPersistence, SUBTITLE_DRAFT_KEY} = await import('/src/subtitles/useDraftPersistence.ts');
const {normalizeSavedDraft} = await import('/src/subtitles/draft.ts');
const {DEFAULT_OPTIONS,createSubtitleDocument} = await import('/src/subtitles/studioConfig.ts');
const {api} = await import('/src/api.ts');
const seed = {...createSubtitleDocument([{id:'c1',text:'original',source_text:'original',source_language:'zh',
  start_ms:0,end_ms:1000,timing_source:'ocr',timing_precision_ms:200,needs_review:false,revision:0}]),
  document_role:'source',language:'zh'};
window.requests=[];window.polls=[];window.sources=[];window.translations=[];window.errors=[];window.notices=[];
localStorage.setItem('content_bot_access_token','fixture');
const originalFetch=window.fetch;
window.fetch=(url,init)=>{
  const match=String(url).match(/\/subtitles\/jobs\/([^/]+)$/);
  if(match)return new Promise(resolve=>window.polls.push({id:match[1],signal:init.signal,
    resolve:data=>resolve(new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}))}));
  return originalFetch(url,init);
};
window.request=signal=>new Promise(resolve=>window.requests.push({signal,resolve}));
function App({restore}) {
  const [videoId,setVideo]=React.useState(restore?.videoId ?? 'aaaaaaaaaaaaaaaa');
  const [document,setDocument]=React.useState(restore ? {...createSubtitleDocument(restore.cues),...restore.documentMeta} : createSubtitleDocument([]));
  const source=useSourceDocument(restore?.sourceDocument ?? seed);
  const [region,setRegion]=React.useState({x:20,y:30,width:60,height:40});
  const [ocrVisible,setOcrVisible]=React.useState(!window.startHidden);
  const jobs=useExtractionJobs({videoId,document,source:source.document,configuration:'fixture',initial:restore?.pendingExtraction,
    onSource:d=>{window.sources.push(d);source.dispatch({type:'replace',document:d});},
    onTranslation:d=>{window.translations.push(d);setDocument(d);},
    onError:e=>{if(e)window.errors.push(e);},onNotice:n=>window.notices.push(n)});
  const draft=React.useMemo(()=>({version:2,videoId,projectName:'fixture',cues:document.segments,
    documentMeta:{revision:document.revision??0,run_id:document.run_id??null},sourceDocument:source.document,pendingExtraction:jobs.pending}),
    [videoId,document,source.document,jobs.pending]);
  useDraftPersistence(draft);
  Object.assign(window,{jobs,setVideo,source,region,seed,setOcrVisible});
  return React.createElement('div',{id:'ocr-frame',style:{position:'relative',width:500,height:300}},React.createElement(SubtitleOcrBox,{region,onChange:setRegion,visible:ocrVisible}));
}
window.mount=restore=>{window.root=ReactDOM.createRoot(document.getElementById('root'));window.root.render(React.createElement(App,{restore}));};
window.restore=()=>normalizeSavedDraft(JSON.parse(localStorage.getItem(SUBTITLE_DRAFT_KEY)),DEFAULT_OPTIONS);
window.mount();
</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/subtitle-remediation/phase1/browser.json"),
    )
    args = parser.parse_args()
    checks, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route(
            args.url + "/extraction-hooks",
            lambda route: route.fulfill(content_type="text/html", body=HTML),
        )
        page.goto(args.url + "/extraction-hooks")
        page.wait_for_function("window.jobs !== undefined")
        page.evaluate("void jobs.start('ocr',request);void jobs.start('ocr',request)")
        page.wait_for_function("requests.length===1 && jobs.running")
        assert page.evaluate("requests.length") == 1
        checks.append("submit is single flight before receiving job id")
        page.evaluate("setVideo('bbbbbbbbbbbbbbbb')")
        page.wait_for_function("requests[0].signal.aborted && !jobs.running")
        page.evaluate("requests[0].resolve({id:'old',state:'queued'})")
        page.wait_for_timeout(50)
        assert page.evaluate("jobs.pending===null && polls.length===0")
        checks.append("late submit is ignored after video change")
        page.evaluate("void jobs.start('translation',request)")
        page.wait_for_function("requests.length===2")
        page.evaluate("requests[1].resolve({id:'translation',state:'queued'})")
        page.wait_for_function("polls.length===1")
        page.evaluate("source.dispatch({type:'edit',id:'c1',patch:{text:'corrected'}})")
        page.wait_for_function("source.document.segments[0].source_text==='corrected'")
        page.evaluate(
            "polls[0].resolve({id:'translation',state:'succeeded',result:{document:seed}})"
        )
        page.wait_for_function("!jobs.running && notices.length===1")
        assert page.evaluate(
            "translations.length===0 && source.document.segments[0].text==='corrected'"
        )
        checks.append(
            "edit during translation preserves current text and exposes saved result"
        )
        page.evaluate("void jobs.start('ocr',request)")
        page.wait_for_function("requests.length===3")
        page.evaluate("requests[2].resolve({id:'0123456789abcdef0123',state:'queued'})")
        page.wait_for_function(
            "jobs.pending?.id==='0123456789abcdef0123' && polls.length===2"
        )
        page.evaluate("root.unmount();window.saved=restore();mount(saved)")
        page.wait_for_function(
            "polls.length===3 && jobs.pending?.id==='0123456789abcdef0123'"
        )
        assert page.evaluate("polls[1].signal.aborted")
        page.evaluate(
            "polls[2].resolve({id:'0123456789abcdef0123',state:'succeeded',result:{document:seed}})"
        )
        page.wait_for_function("!jobs.running")
        assert page.evaluate("sources.length===1 && notices.length===1")
        checks.append(
            "unmount flushes draft and matching pending job resumes after normalization"
        )
        page.evaluate("void jobs.start('ocr',request)")
        page.wait_for_function("requests.length===4")
        page.evaluate("requests[3].resolve({id:'oldpoll',state:'queued'})")
        page.wait_for_function("polls.length===4")
        page.evaluate("setVideo('cccccccccccccccc')")
        page.wait_for_function("polls[3].signal.aborted && !jobs.running")
        page.evaluate(
            "polls[3].resolve({id:'oldpoll',state:'succeeded',result:{document:seed}})"
        )
        page.wait_for_timeout(50)
        assert page.evaluate("sources.length===1")
        checks.append("late completed job is ignored after switching video")
        # Pointer events on the actual component: resize from top-left, keeping bottom-right fixed.
        page.evaluate("""() => {
          const h=document.querySelector('.is-nw');
          h.setPointerCapture=()=>{};
          h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1,clientX:100,clientY:90}));
          h.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:1,clientX:150,clientY:120}));
          h.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:1}));
        }""")
        page.wait_for_function("region.x===30 && region.y===40")
        assert page.evaluate("region.width===50 && region.height===30")
        checks.append(
            "northwest OCR resize moves both axes and preserves opposite corner"
        )
        page.locator("[role=button]").press("ArrowUp")
        page.wait_for_function("region.y===39.5")
        checks.append("OCR region keyboard navigation works")
        page.evaluate("void jobs.start('ocr',request)")
        page.wait_for_function("requests.length===5")
        page.evaluate("root.unmount()")
        assert page.evaluate("requests[4].signal.aborted")
        page.evaluate("requests[4].resolve({id:'unmounted',state:'queued'})")
        page.wait_for_timeout(50)
        assert not errors, errors
        assert page.evaluate("errors.length") == 0
        checks.append("unmount aborts submit without errors or stray polling")
        page.evaluate("window.startHidden=true;mount();")
        page.wait_for_function("document.querySelector('#ocr-frame') !== null")
        assert page.locator('.subtitle-ocr-box').count() == 0
        page.evaluate("setOcrVisible(true)")
        page.wait_for_function("parseFloat(document.querySelector('.subtitle-ocr-box')?.style.width)===300")
        checks.append("OCR first shown after hidden mount measures its frame")
        page.evaluate("""() => {
          const frame=document.querySelector('#ocr-frame');
          const video=document.createElement('video');
          Object.defineProperties(video,{videoWidth:{value:1920},videoHeight:{value:1080}});
          frame.prepend(video);
          video.dispatchEvent(new Event('loadedmetadata'));
        }""")
        page.wait_for_function("parseFloat(document.querySelector('.subtitle-ocr-box').style.top)===93.75")
        assert page.evaluate("parseFloat(document.querySelector('.subtitle-ocr-box').style.height)") == 112.5
        checks.append("new video metadata maps OCR into the letterboxed image")
        page.evaluate("""() => {
          const h=document.querySelector('.is-se'); h.setPointerCapture=()=>{};
          h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:2,clientX:100,clientY:100}));
          h.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:2,clientX:150,clientY:128.125}));
          h.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:2}));
        }""")
        page.wait_for_function("region.width===70 && region.height===50")
        checks.append("letterboxed OCR resize uses image dimensions on both axes")
        page.evaluate("document.querySelector('#ocr-frame').style.width='1000px'")
        page.wait_for_function("parseFloat(document.querySelector('.subtitle-ocr-box').style.width)===373.3333333333333 || Math.abs(parseFloat(document.querySelector('.subtitle-ocr-box').style.width)-373.3333333333333)<0.001")
        checks.append("OCR resize observer tracks pillarbox after frame resize")
        page.evaluate("root.unmount()")
        assert not errors, errors
        browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"checks": checks, "page_errors": errors}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"passed": len(checks), "artifact": str(args.output)}))


if __name__ == "__main__":
    main()
