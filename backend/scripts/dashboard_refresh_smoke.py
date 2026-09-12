"""Real React hook smoke in Chromium with an isolated in-memory API; requires Vite."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HTML = r'''<!doctype html><html><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/features/topics/useDashboardData.ts')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useDashboardData} = await import('/src/features/topics/useDashboardData.ts');
const originalFetch = window.fetch;
window.calls = []; window.current = {}; window.peak = {}; window.delay = 0;
window.fetch = async (url, init) => {
  if (!String(url).includes('/api/v1/')) return originalFetch(url, init);
  const parsed = new URL(url); const keyword = Number(parsed.searchParams.get('keyword_id'));
  const key = parsed.pathname + '?' + keyword;
  window.calls.push(key); window.current[key] = (window.current[key] || 0) + 1;
  window.peak[key] = Math.max(window.peak[key] || 0, window.current[key]);
  // Deliberately ignores AbortSignal to verify the hook rejects stale results.
  await new Promise(resolve => setTimeout(resolve, window.delay));
  window.current[key]--;
  const body = parsed.pathname.endsWith('/items') ? {total:1, items:[{id:keyword,title:'keyword-'+keyword}]}
    : parsed.pathname.endsWith('/runs') ? [] : {total_items:keyword};
  return new Response(JSON.stringify(body), {headers:{'Content-Type':'application/json'}});
};
function App() {
  const [keyword, setKeyword] = React.useState(1);
  const [enabled, setEnabled] = React.useState(true);
  const [refreshKey, refresh] = React.useState(0);
  const data = useDashboardData({keywordId:keyword, filters:{}, enabled, refreshKey,
    onError:error => { throw new Error(error); }, onCompleted:()=>{}, onAuthRequired:async()=>false});
  window.change = setKeyword; window.enable = setEnabled; window.refresh = () => refresh(value=>value+1);
  return React.createElement('output', {id:'state'}, JSON.stringify({keyword,items:data.items.map(item=>item.id),loading:data.loading}));
}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--output", type=Path, default=Path("artifacts/refactor-dashboard/browser.json"))
    args = parser.parse_args()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(args.url + "/refactor-dashboard", lambda route: route.fulfill(content_type="text/html", body=HTML))
        page.goto(args.url + "/refactor-dashboard")
        page.wait_for_function("document.querySelector('#state')?.textContent.includes('\\\"items\\\":[1]')".replace('\\"', '"'))
        page.evaluate("window.delay=300; window.change(2)")
        page.wait_for_function("window.calls.some(key=>key.endsWith('?2'))")
        page.evaluate("window.change(3)")
        page.wait_for_function("JSON.parse(document.querySelector('#state').textContent).items[0]===3")
        page.wait_for_timeout(400)
        assert page.locator("#state").text_content().find('"items":[3]') >= 0
        page.evaluate("window.enable(false)")
        page.wait_for_timeout(30)
        count = page.evaluate("window.calls.length")
        page.evaluate("window.refresh()")
        page.wait_for_timeout(500)
        assert page.evaluate("window.calls.length") == count
        page.evaluate("window.enable(true)")
        page.wait_for_function(f"window.calls.length>{count}")
        page.wait_for_function("!JSON.parse(document.querySelector('#state').textContent).loading")
        for _ in range(5):
            page.evaluate("window.refresh()")
            page.wait_for_timeout(20)
        page.wait_for_timeout(1200)
        peaks = page.evaluate("window.peak")
        assert max(peaks.values()) == 1, peaks
        assert errors == [], errors
        report = {"browser": browser.version, "errors": errors, "peak_requests_per_query_endpoint": peaks,
                  "calls": page.evaluate("window.calls"), "state": json.loads(page.locator("#state").text_content())}
        browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "browser": report["browser"], "requests": len(report["calls"])}))


if __name__ == "__main__":
    main()
