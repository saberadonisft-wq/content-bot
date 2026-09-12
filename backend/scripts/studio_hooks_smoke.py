"""Exercise actual editor polling and persistence hooks in Chromium via Vite."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HTML = r'''<!doctype html><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/subtitles/useJobPolling.ts')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useJobPolling} = await import('/src/subtitles/useJobPolling.ts');
const {useDraftPersistence, SUBTITLE_DRAFT_KEY} = await import('/src/subtitles/useDraftPersistence.ts');
const {useSubtitleImport} = await import('/src/subtitles/useSubtitleImport.ts');
const {useRenderedVideoDownload} = await import('/src/subtitles/useRenderedVideoDownload.ts');
window.requests = []; window.results = []; window.errors = [];
window.imports = []; window.parsed = []; window.downloads = [];
const originalFetch = window.fetch;
window.fetch = (url, init) => String(url).endsWith('/subtitles/v2/parse')
  ? new Promise(resolve => window.imports.push({signal:init.signal, resolve})) : originalFetch(url, init);
window.contentBotDesktop = {downloadFile: async options => { window.downloads.push(options); return {state:'completed'}; }};
localStorage.setItem('content_bot_access_token', 'fixture');
const fetchJob = (id, signal) => new Promise(resolve => window.requests.push({id, signal, resolve}));
function App() {
  const [id, setId] = React.useState('a');
  const [label, setLabel] = React.useState('first');
  const draft = React.useMemo(() => ({version:2, projectName:label}), [label]);
  useDraftPersistence(draft);
  useJobPolling({jobId:id, fetchJob, interval:20, retryDelay:40,
    onJob:job => window.results.push({id:job.id, label}), onError:e => window.errors.push(String(e))});
  const importer = useSubtitleImport({text:'fixture', durationMs:1000,
    onParsed:result=>window.parsed.push(result), onError:e=>{if(e) window.errors.push(e);}});
  const downloader = useRenderedVideoDownload('http://127.0.0.1:8000/api/v1/subtitles/download/result.mp4', 'result.mp4',
    e=>{if(e) window.errors.push(e);});
  window.parse = importer.parse; window.download = downloader.download;
  window.downloadMessage = downloader.message;
  window.setId = setId; window.setLabel = setLabel;
  return React.createElement('output', null, id+':'+label);
}
window.root = ReactDOM.createRoot(document.getElementById('root'));
window.root.render(React.createElement(App));
window.draftKey = SUBTITLE_DRAFT_KEY;
</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--output", type=Path, default=Path("artifacts/refactor-modules/studio-hooks.json"))
    args = parser.parse_args()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(args.url + "/studio-hooks", lambda route: route.fulfill(content_type="text/html", body=HTML))
        page.goto(args.url + "/studio-hooks")
        page.wait_for_function("window.requests?.length === 1")
        page.evaluate("window.setLabel('latest')")
        page.wait_for_function("document.querySelector('output').textContent === 'a:latest'")
        assert page.evaluate("window.requests.length") == 1
        page.evaluate("window.requests[0].resolve({id:'a',state:'running'})")
        page.wait_for_function("window.requests.length === 2")
        assert page.evaluate("window.results[0].label") == "latest"
        page.evaluate("window.setId('b')")
        page.wait_for_function("window.requests.length === 3")
        assert page.evaluate("window.requests[1].signal.aborted")
        page.evaluate("window.requests[1].resolve({id:'a',state:'succeeded'}); window.requests[2].resolve({id:'b',state:'succeeded'})")
        page.wait_for_function("window.results.length === 2")
        page.wait_for_function("JSON.parse(localStorage.getItem(window.draftKey))?.projectName === 'latest'")
        assert page.evaluate("window.requests.length") == 3
        assert page.evaluate("window.results.map(x=>x.id)") == ["a", "b"]
        page.evaluate("void window.parse(); void window.parse()")
        page.wait_for_function("window.imports.length === 2")
        assert page.evaluate("window.imports[0].signal.aborted")
        page.evaluate("window.imports[0].resolve(new Response(JSON.stringify({id:'stale'}))); window.imports[1].resolve(new Response(JSON.stringify({id:'current'})))")
        page.wait_for_function("window.parsed.length === 1")
        assert page.evaluate("window.parsed[0].id") == "current"
        page.evaluate("window.download()")
        page.wait_for_function("window.downloadMessage === 'Đã tải video'")
        assert page.evaluate("window.downloads[0].accessToken") == "fixture"
        page.evaluate("window.root.unmount()")
        assert page.evaluate("window.requests.every(request=>request.signal.aborted)")
        assert errors == [] and page.evaluate("window.errors.length") == 0
        report = {"browser": browser.version, "errors": errors, "requests": 3,
                  "verified": ["single_flight", "latest_callback", "stale_response", "terminal_stop", "unmount_abort", "debounced_draft", "import_cancel_stale", "desktop_export_auth"]}
        browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
