# -*- coding: utf-8 -*-
"""
dm_sweep_local.py: sends the comment->DM links WITHOUT Claude (no tokens).
Runs on this PC (Windows Task Scheduler). Talks to Instagram through Composio's MCP endpoint over plain HTTPS.

  python dm_sweep_local.py --check   # test the key + the Instagram connection (sends nothing)
  python dm_sweep_local.py --dry     # show who WOULD get a DM (sends nothing)
  python dm_sweep_local.py           # real run: DM + public reply to everyone who commented a keyword

Reads: .env (COMPOSIO_API_KEY=ck_... and INSTAGRAM_USERNAME=yourname), dm_campaigns.json.   Writes: dm_sent.json, dm_sweep.log.
Python standard library only.
"""
import json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://connect.composio.dev/mcp"
SENT_FILE = os.path.join(HERE, "dm_sent.json")
LOG_FILE = os.path.join(HERE, "dm_sweep.log")
LOCK_FILE = os.path.join(HERE, ".running.lock")
MAX_RUN_SECONDS = 20 * 60
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    env = dict(os.environ)
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8-sig"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = load_env()
KEY = _ENV.get("COMPOSIO_API_KEY", "")
OWN = _ENV.get("INSTAGRAM_USERNAME", "")   # your own username, so your own comments never trigger a DM
_id = [0]


def rpc(method, params=None, retries=2):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params or {}}
    for attempt in range(retries + 1):
        req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                              "x-consumer-api-key": KEY, "User-Agent": "dm-sweep/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read().decode("utf-8", "replace")
            for ln in body.splitlines():
                if ln.startswith("data:"):
                    return json.loads(ln[5:].strip())
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(4 * (attempt + 1)); continue
            return {"error": {"http": e.code, "body": e.read().decode("utf-8", "replace")[:200]}}
        except Exception as e:
            if attempt < retries:
                time.sleep(4); continue
            return {"error": {"exception": str(e)[:200]}}


def mcp_tool(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    res = r.get("result") or {}
    txt = "".join(c.get("text", "") for c in res.get("content", []))
    try:
        return json.loads(txt)
    except Exception:
        return {"successful": False, "error": r.get("error") or txt[:300]}


def tool(slug, args):
    """Run one Composio toolkit tool. Returns (ok, data)."""
    r = mcp_tool("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": slug, "arguments": args}], "sync_response_to_workbench": False})
    try:
        resp = r["data"]["results"][0]["response"]
        return bool(resp.get("successful")), resp.get("data"), resp.get("error")
    except Exception:
        return False, None, r.get("error") or r.get("raw")


def find_status(d):
    for _ in range(5):
        if isinstance(d, dict):
            if isinstance(d.get("status"), int):
                return d["status"], d.get("data")
            d = d.get("data")
        else:
            break
    return None, d


def proxy(method, endpoint, query=None, body=None):
    args = {"toolkit_slug": "instagram", "endpoint": endpoint, "method": method}
    if query:
        args["parameters"] = [{"name": k, "value": str(v), "type": "query"} for k, v in query.items()]
    if body is not None:
        args["body"] = body
    r = mcp_tool("COMPOSIO_PROXY_EXECUTE", args)
    status, data = find_status(r)
    return status, data, r


def unwrap(d):
    """Find the dict that holds a list under 'data' (a Graph API page), whatever the nesting."""
    for _ in range(5):
        if not isinstance(d, dict):
            return {}
        if isinstance(d.get("data"), list):
            return d
        d = d.get("data")
    return {}


def paged(slug, base_args, max_pages=60):
    out, after = [], None
    for _ in range(max_pages):
        a = dict(base_args)
        if after:
            a["after"] = after
        ok, data, err = tool(slug, a)
        if not ok:
            log("%s error: %s" % (slug, json.dumps(err, ensure_ascii=False)[:200])); break
        page = unwrap(data)
        items = page.get("data", [])
        out += items
        pg = page.get("paging") or {}
        nxt = (pg.get("cursors") or {}).get("after")
        if not items or not nxt or nxt == after:
            break
        after = nxt
    return out


def ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def known_dm_people():
    items = paged("INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 100, "fields": "participants", "platform": "instagram"}, 80)
    people = set()
    for c in items:
        for p in ((c.get("participants") or {}).get("data") or []):
            people.add(str(p.get("id"))); people.add(p.get("username"))
    return people, len(items)


def main():
    mode = "check" if "--check" in sys.argv else "dry" if "--dry" in sys.argv else "send"
    if not KEY or "PASTE" in KEY or not OWN:
        print("Put COMPOSIO_API_KEY and INSTAGRAM_USERNAME in .env next to this file."); return 2
    if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) < MAX_RUN_SECONDS:
        log("another run is active; skipping"); return 0
    open(LOCK_FILE, "w").write(str(os.getpid()))
    try:
        return run(mode)
    finally:
        try: os.remove(LOCK_FILE)
        except Exception: pass


def run(mode):
    t0 = time.time()
    rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dm-sweep", "version": "1.0"}})
    ok, data, err = tool("INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me", "fields": "username,followers_count"})
    if mode == "check":
        log("CHECK instagram_ok=%s username=%s followers=%s" % (ok, (data or {}).get("username"), (data or {}).get("followers_count")))
        if not ok:
            log("error: %s" % json.dumps(err, ensure_ascii=False)[:300]); return 4
        return 0
    if not ok:
        log("instagram not reachable: %s" % json.dumps(err, ensure_ascii=False)[:200]); return 4
    camps = json.load(open(os.path.join(HERE, "dm_campaigns.json"), encoding="utf-8"))
    sent = json.load(open(SENT_FILE, encoding="utf-8")) if os.path.exists(SENT_FILE) else {}
    cut = datetime.now(timezone.utc) - timedelta(days=7)
    media = paged("INSTAGRAM_GET_IG_USER_MEDIA", {"ig_user_id": "me", "fields": "id,caption,timestamp", "limit": 25}, 1)
    people, n_conv = known_dm_people()
    if n_conv == 0:
        log("could not read DM conversations; stopping to avoid double sends"); return 5
    todo, seen = [], set()
    for ci, cp in enumerate(camps):
        mids = set(cp["media_ids"]) | {m["id"] for m in media
                                       if any(k.lower() in (m.get("caption") or "").lower() for k in cp["caption_match"])
                                       and ts(m["timestamp"]) > cut}
        rx = re.compile(cp["regex"])
        for mid in mids:
            cs = paged("INSTAGRAM_GET_IG_MEDIA_COMMENTS", {"ig_media_id": mid, "fields": "id,text,timestamp,from", "limit": 100})
            for c in sorted(cs, key=lambda c: c["timestamp"]):
                f = c.get("from") or {}
                u = f.get("username")
                if u == OWN or c["id"] in sent or ts(c["timestamp"]) < cut or not rx.search((c.get("text") or "").lower()):
                    continue
                if u in people or str(f.get("id")) in people or u in seen:
                    continue
                seen.add(u); todo.append((ci, c))
    log("mode=%s campaigns=%d dm_threads=%d to_serve=%d" % (mode, len(camps), n_conv, len(todo)))
    if mode == "dry":
        for ci, c in todo[:15]:
            print("  would DM", (c.get("from") or {}).get("username"), "|", (c.get("text") or "")[:20])
        return 0
    ok_n = err_n = 0
    for ci, c in todo:
        if time.time() - t0 > MAX_RUN_SECONDS:
            log("time budget reached; the rest goes next run"); break
        cp = camps[ci]
        status, d, raw = proxy("POST", "/me/messages", body={"recipient": {"comment_id": c["id"]}, "message": {"text": cp["msg"]}})
        if status == 200:
            ok_n += 1; sent[c["id"]] = {"dm": "ok", "camp": cp["name"], "at": datetime.now().isoformat(timespec="seconds")}
            tool("INSTAGRAM_POST_IG_COMMENT_REPLIES", {"ig_comment_id": c["id"], "message": cp["replies"][len(sent) % len(cp["replies"])]})
        else:
            err_n += 1; sent[c["id"]] = {"dm": "err", "status": status}
            txt = json.dumps(raw, ensure_ascii=False)[:300]
            log("DM error %s: %s" % (status, txt))
            if re.search(r"(?i)rate|limit|spam|block|throttl", txt):
                log("stopping: Instagram asked to slow down"); break
        json.dump(sent, open(SENT_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        time.sleep(2.5)
    log("DONE new=%d errors=%d total_ok=%d" % (ok_n, err_n, sum(1 for v in sent.values() if v.get("dm") == "ok")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
