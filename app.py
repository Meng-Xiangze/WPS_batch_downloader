# -*- coding: utf-8 -*-
"""
WPS 365 Batch Downloader - Pure web UI, headless login
"""
import asyncio, base64, json, os, threading, time, re, io, socket
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, Response

BASE_DIR = Path(__file__).parent
TEMPLATES = BASE_DIR / "templates"
DOWNLOADS = BASE_DIR / "downloads"
STATE_FILE = BASE_DIR / "state.json"
DOWNLOADS.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES))

# ── Event loop ────────────────────────────────────────
_loop = asyncio.new_event_loop()
def _run_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()
threading.Thread(target=_run_loop, daemon=True).start()
def _async(coro, timeout=60):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

# ── State ─────────────────────────────────────────────
_lock = threading.Lock()
_pw = None
_browser = None
_ctx = None
_page = None
_pause = threading.Event()
_pause.set()
_login_done = threading.Event()

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            s.setdefault("tasks", [])
            s.setdefault("groups", [])
            s.setdefault("download_dir", "")
            s.setdefault("browser", {"running": False, "logged_in": False})
            return s
    return {"tasks": [], "groups": [], "download_dir": "", "browser": {"running": False, "logged_in": False}}

def choose_download_dir():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="选择下载保存目录", mustexist=True) or ""
    finally:
        root.destroy()

def safe_dir_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "")).strip(" .")
    return name[:120] or f"task_{int(time.time())}"

def save_state(s):
    with _lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)

# ── Headless browser for login + download ─────────────
LOGIN_URL = "https://account.wps.cn/?from=v1-web-kdocs-login&cb=https%3A%2F%2Faccount.kdocs.cn%2Fpassport%2Fsinglesign%3Fcb%3Dhttps%253A%252F%252Fwww.kdocs.cn%252Flatest%253Ffrom%253Ddocs%26form%3Dwww.kdocs.cn"

async def _launch():
    global _pw, _browser, _ctx, _page
    from playwright.async_api import async_playwright
    
    if _pw is None:
        _pw = await async_playwright().start()
    
    _browser = await _pw.chromium.launch(
        headless=True,
        args=["--no-first-run", "--no-default-browser-check", "--no-sandbox"]
    )
    _ctx = await _browser.new_context(viewport={"width": 1100, "height": 760})
    _page = await _ctx.new_page()
    await _page.goto(LOGIN_URL, wait_until="networkidle")
    
    # Monitor for login completion
    async def _on_nav(frame):
        if frame == _page.main_frame:
            url = frame.url
            if "kdocs.cn" in url and "account" not in url and "passport" not in url:
                _login_done.set()
                s = load_state()
                s["browser"]["logged_in"] = True
                save_state(s)
    _page.on("framenavigated", _on_nav)

async def _check_login():
    if _page is None: return False, "no browser"
    url = _page.url
    if _login_done.is_set(): return True, url
    if "kdocs.cn" in url and "account" not in url:
        _login_done.set()
        return True, url
    return False, url

async def _goto_url(url):
    global _page
    await _page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    return _page.url

# ── QR screenshot endpoint ────────────────────────────
@app.route("/api/login_qr.png")
def api_login_qr():
    global _page
    if _page is None:
        # Return placeholder
        return Response("no browser", status=503)
    try:
        # Take screenshot of current page (showing QR code)
        png = _async(_page.screenshot(type="png"))
        return Response(png, mimetype="image/png")
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/api/login/click", methods=["POST"])
def api_login_click():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Login page is not ready"})
    data = request.json or {}
    try:
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
        _async(_page.mouse.click(x, y), timeout=10)
        _async(asyncio.sleep(1), timeout=3)
        logged_in, info = _async(_check_login(), timeout=10)
        return jsonify({"ok": True, "logged_in": logged_in, "info": info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/browser/goto", methods=["POST"])
def api_browser_goto():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Browser not launched"})
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL"})
    try:
        final_url = _async(_goto_url(url), timeout=60)
        return jsonify({"ok": True, "url": final_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/browser/current")
def api_browser_current():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Browser not launched"})
    try:
        return jsonify({"ok": True, "url": _page.url, "title": _async(_page.title(), timeout=10)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/browser/wheel", methods=["POST"])
def api_browser_wheel():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Browser not launched"})
    data = request.json or {}
    try:
        dx = float(data.get("dx", 0))
        dy = float(data.get("dy", 0))
        _async(_page.mouse.wheel(dx, dy), timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── File discovery ────────────────────────────────────
async def _discover(groupid, parentid=0, prefix=""):
    global _page
    api_url = f"https://drive.kdocs.cn/api/v5/groups/{groupid}/files?parentid={parentid}&include=acl,pic_thumbnail&with_link=true"
    
    r = await _page.evaluate("""
        async (url) => { const r=await fetch(url,{credentials:'include'}); const d=await r.json(); return {ok:true, data:d}; }
    """, api_url)
    
    items = r["data"].get("files", [])
    found = []
    for it in items:
        nm, tp, fid, sz = it["fname"], it["ftype"], it["id"], it.get("fsize", 0)
        if tp == "file":
            ext = nm.rsplit(".", 1)[-1].lower() if "." in nm else ""
            if ext != "otl":
                found.append({"name": nm, "id": fid, "size": sz, "path": f"{prefix}{nm}", "status": "pending"})
        elif tp == "folder":
            found.extend(await _discover(groupid, fid, f"{prefix}{nm}/"))
    return found

async def _create_task_from_page(url=None):
    global _page
    if url:
        await _goto_url(url)
    current_url = _page.url
    m = re.search(r'/ent/\d+/(\d+)', current_url)
    if not m:
        return None, f"当前页面不是团队文档页：{current_url}"
    groupid = int(m.group(1))
    title = await _page.title()
    files = await _discover(groupid)
    return {
        "id": f"task_{int(time.time())}", "url": current_url, "name": title or current_url,
        "groupid": groupid, "status": "analyzed", "files": files,
        "total_files": len(files), "total_size": sum(f["size"] for f in files),
        "downloaded_files": 0, "downloaded_size": 0,
        "created_at": datetime.now().isoformat()
    }, None

async def _discover_entgroups():
    global _page
    response_texts = []

    async def _capture_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype and "text" not in ctype:
                return
            if "kdocs.cn" not in resp.url:
                return
            text = await resp.text()
            if len(text) < 2_000_000:
                response_texts.append(text)
        except Exception:
            pass

    def _on_response(resp):
        asyncio.create_task(_capture_response(resp))

    _page.on("response", _on_response)
    await _page.goto("https://www.kdocs.cn/entgroups", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    raw = await _page.evaluate("""
        () => {
            const out = [];
            const push = (url, name) => {
                if (!url) return;
                try { url = new URL(url, location.href).href; } catch (e) {}
                out.push({url, name: (name || '').replace(/\\s+/g, ' ').trim()});
            };
            document.querySelectorAll('a[href]').forEach(a => {
                push(a.getAttribute('href'), a.innerText || a.textContent || a.getAttribute('title'));
            });
            document.querySelectorAll('[data-url],[data-href]').forEach(el => {
                push(el.getAttribute('data-url') || el.getAttribute('data-href'), el.innerText || el.textContent || el.getAttribute('title'));
            });
            const html = document.documentElement.innerHTML;
            for (const m of html.matchAll(/(?:https?:\\/\\/www\\.kdocs\\.cn)?\\/ent\\/\\d+\\/\\d+[^"'<>\\s]*/g)) {
                push(m[0], '');
            }
            return out;
        }
    """)
    for text in response_texts:
        for m in re.finditer(r'(?:https?:\\?/\\?/www\\.kdocs\\.cn)?\\?/ent\\?/(\d+)\\?/(\d+)', text):
            raw.append({"url": f"https://www.kdocs.cn/ent/{m.group(1)}/{m.group(2)}", "name": ""})
    groups = []
    seen = set()
    for item in raw:
        url = item.get("url", "")
        m = re.search(r"https?://www\.kdocs\.cn/ent/(\d+)/(\d+)", url)
        if not m:
            m = re.search(r"/ent/(\d+)/(\d+)", url)
            if m:
                url = f"https://www.kdocs.cn/ent/{m.group(1)}/{m.group(2)}"
        if not m:
            continue
        entid, groupid = m.group(1), m.group(2)
        key = f"{entid}:{groupid}"
        if key in seen:
            continue
        seen.add(key)
        name = item.get("name") or f"团队 {groupid}"
        groups.append({"name": name, "entid": entid, "groupid": int(groupid), "url": f"https://www.kdocs.cn/ent/{entid}/{groupid}"})
    return groups

# ── Download ──────────────────────────────────────────
async def _dl_one(groupid, fid, save_path):
    global _page
    api_url = f"https://drive.kdocs.cn/api/v5/groups/{groupid}/files/{fid}/download?support_checksums=1"
    r = await _page.evaluate("""
        async (url) => { const r=await fetch(url,{credentials:'include'}); const d=await r.json(); return d.url?{ok:true, url:d.url}:{ok:false}; }
    """, api_url)
    if not r["ok"]: return False
    r2 = await _page.evaluate("""
        async (url) => { const r=await fetch(url,{credentials:'include'}); const b=await r.blob(); const ab=await b.arrayBuffer(); const u8=new Uint8Array(ab); let s=''; for(let i=0;i<u8.length;i++) s+=String.fromCharCode(u8[i]); return {ok:true, b64:btoa(s)}; }
    """, r["url"])
    if r2["ok"]:
        with open(save_path, "wb") as f:
            f.write(base64.b64decode(r2["b64"]))
        return True
    return False

# ── Routes ────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def api_state():
    return jsonify(load_state())

@app.route("/api/browser/launch", methods=["POST"])
def api_launch():
    try:
        if _page:
            try:
                _async(_page.close())
            except:
                pass
        if _ctx:
            try:
                _async(_ctx.close())
            except:
                pass
        if _browser:
            try:
                _async(_browser.close())
            except:
                pass
        _login_done.clear()
        _async(_launch())
        s = load_state()
        s["browser"]["running"] = True
        save_state(s)
        return jsonify({"ok": True, "message": "QR code loaded. Scan to login."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/browser/status")
def api_status():
    global _page
    if _page is None:
        return jsonify({"running": False, "logged_in": False})
    try:
        logged_in, info = _async(_check_login())
        s = load_state()
        s["browser"]["running"] = True
        s["browser"]["logged_in"] = logged_in
        save_state(s)
        return jsonify({"running": True, "logged_in": logged_in, "info": info})
    except:
        return jsonify({"running": False, "logged_in": False})

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    global _page
    data = request.json
    url = data.get("url", "").strip()
    if not url: return jsonify({"ok": False, "error": "No URL"})
    if _page is None: return jsonify({"ok": False, "error": "Browser not launched"})
    
    try:
        task, error = _async(_create_task_from_page(url), timeout=180)
        if error:
            return jsonify({"ok": False, "error": error})
        s = load_state()
        s["tasks"].append(task)
        save_state(s)
        return jsonify({"ok": True, "task": task})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/analyze_current", methods=["POST"])
def api_analyze_current():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Browser not launched"})
    try:
        task, error = _async(_create_task_from_page(), timeout=180)
        if error:
            return jsonify({"ok": False, "error": error})
        s = load_state()
        s["tasks"].append(task)
        save_state(s)
        return jsonify({"ok": True, "task": task})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/groups/discover", methods=["POST"])
def api_groups_discover():
    global _page
    if _page is None:
        return jsonify({"ok": False, "error": "Browser not launched"})
    try:
        groups = _async(_discover_entgroups(), timeout=90)
        s = load_state()
        s["groups"] = groups
        save_state(s)
        return jsonify({"ok": True, "groups": groups})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/set_task", methods=["POST"])
def api_set_task():
    data = request.json
    s = load_state()
    for i, t in enumerate(s["tasks"]):
        if t["id"] == data["id"]:
            s["tasks"][i] = data; save_state(s)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not found"})

@app.route("/api/download_dir", methods=["POST"])
def api_download_dir():
    try:
        folder = choose_download_dir()
        if not folder:
            return jsonify({"ok": False, "error": "未选择下载目录"})
        s = load_state()
        s["download_dir"] = folder
        save_state(s)
        return jsonify({"ok": True, "download_dir": folder})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/download/<task_id>", methods=["POST"])
def api_download(task_id):
    s = load_state()
    task = next((t for t in s["tasks"] if t["id"] == task_id), None)
    if not task: return jsonify({"ok": False, "error": "Not found"})
    if task["status"] == "downloading": return jsonify({"ok": False, "error": "Already downloading"})
    data = request.json or {}
    download_dir = data.get("download_dir") or s.get("download_dir")
    if not download_dir:
        return jsonify({"ok": False, "error": "No download directory"})
    task["download_dir"] = download_dir
    s["download_dir"] = download_dir
    task["status"] = "downloading"; save_state(s)
    asyncio.run_coroutine_threadsafe(_download_task(task_id), _loop)
    return jsonify({"ok": True})

@app.route("/api/download/<task_id>/pause", methods=["POST"])
def api_pause(task_id):
    _pause.clear()
    s = load_state()
    t = next((t for t in s["tasks"] if t["id"] == task_id), None)
    if t: t["status"] = "paused"; save_state(s)
    return jsonify({"ok": True})

@app.route("/api/download/<task_id>/resume", methods=["POST"])
def api_resume(task_id):
    _pause.set()
    s = load_state()
    t = next((t for t in s["tasks"] if t["id"] == task_id), None)
    if t and t["status"] == "paused":
        t["status"] = "downloading"; save_state(s)
        asyncio.run_coroutine_threadsafe(_download_task(task_id), _loop)
    return jsonify({"ok": True})

@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete(task_id):
    s = load_state()
    s["tasks"] = [t for t in s["tasks"] if t["id"] != task_id]
    save_state(s)
    return jsonify({"ok": True})

# ── Download worker ───────────────────────────────────
async def _download_task(task_id):
    global _page
    s = load_state()
    task = next((t for t in s["tasks"] if t["id"] == task_id), None)
    if not task: return
    groupid = task["groupid"]
    base_out = Path(task.get("download_dir") or DOWNLOADS)
    outdir = base_out / safe_dir_name(task.get("name") or task_id)
    outdir.mkdir(exist_ok=True)
    pending = [f for f in task["files"] if f["status"] == "pending"]
    for fi in pending:
        while not _pause.is_set(): await asyncio.sleep(1)
        fid, fname, fpath = fi["id"], fi["name"], fi["path"]
        fd = outdir
        if "/" in fpath:
            for p in fpath.split("/")[:-1]: fd = fd / p; fd.mkdir(exist_ok=True)
        target = fd / fname
        if target.exists() and target.stat().st_size > 0:
            fi["status"] = "done"; fi["downloaded_size"] = target.stat().st_size
        else:
            try:
                ok = await _dl_one(groupid, fid, str(target))
                fi["status"] = "done" if ok else "failed"
                if ok: fi["downloaded_size"] = target.stat().st_size
            except Exception as e:
                fi["status"] = "failed"; fi["error"] = str(e)
        task["downloaded_files"] = sum(1 for f in task["files"] if f["status"] == "done")
        task["downloaded_size"] = sum(f.get("downloaded_size", 0) for f in task["files"] if f["status"] == "done")
        save_state(s)
        await asyncio.sleep(0.1)
    s = load_state()
    task = next((t for t in s["tasks"] if t["id"] == task_id), None)
    if task:
        failed = [f for f in task["files"] if f["status"] == "failed"]
        task["status"] = "done_with_errors" if failed else "done"
        save_state(s)

# ── Main ──────────────────────────────────────────────
def _find_free_port(start=5000):
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free local port found")


def _start_headless_login():
    def _auto():
        time.sleep(1)
        try:
            asyncio.run_coroutine_threadsafe(_launch(), _loop).result(timeout=60)
            s = load_state()
            s["browser"]["running"] = True
            save_state(s)
            print("  Login page ready - scan QR inside the app window")
        except Exception as e:
            print(f"  Auto-launch error: {e}")
    threading.Thread(target=_auto, daemon=True).start()


def _run_flask(port):
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


def run_desktop_app():
    port = _find_free_port(5000)
    url = f"http://127.0.0.1:{port}"

    print(f"\n{'='*60}")
    print("  WPS 365 Batch Downloader")
    print("  Mode: embedded web app window")
    print(f"  Local UI: {url}")
    print(f"{'='*60}")

    _start_headless_login()
    threading.Thread(target=_run_flask, args=(port,), daemon=True).start()
    time.sleep(1)

    try:
        import webview
        webview.create_window(
            "WPS 365 Batch Downloader",
            url,
            width=1220,
            height=820,
            min_size=(980, 680),
        )
        webview.start()
    except Exception as e:
        print(f"  Embedded window failed: {e}")
        print(f"  Fallback UI: {url}")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    run_desktop_app()
