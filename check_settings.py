"""CI smoke test of the webmail's controls (no IMAP/DAV needed): tools listed and
refused per settings.json, every call recorded in calls.jsonl, history cleared."""
import asyncio, importlib.util, json, os, pathlib, sys, tempfile

base = pathlib.Path(tempfile.mkdtemp())
(base / "settings").mkdir(); (base / "log").mkdir()
os.environ.update(MAILHUB_PASSWORD="dummy", MAILHUB_MCP_SETTINGS=str(base / "settings"),
                  MAILHUB_MCP_LOG_DIR=str(base / "log"), MAILHUB_ROUNDCUBE_DB=str(base / "none.db"))
os.environ.pop("MAILHUB_MCP_WRITE", None)
spec = importlib.util.spec_from_file_location("server", pathlib.Path(__file__).with_name("server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
failures = []


def check(label, ok, detail=None):
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  [{detail}]"))
    ok or failures.append(label)


def put(**conf):
    path = base / "settings" / "settings.json"
    path.write_text(json.dumps(conf))
    st = path.stat()  # a new mtime even within the same second
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000 * (len(failures) + put.n)))
    put.n += 1
put.n = 1


async def names():
    return sorted(t.name for t in await mod.server.list_tools())


async def call(name, **args):
    try:
        r = await mod.server.call_tool(name, args)
        return "error" if getattr(r, "is_error", False) else "ok"
    except Exception as e:
        return f"raised: {e}"


def calls():
    p = base / "log" / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


async def main():
    mod.write_catalog()
    cat = json.loads((base / "log" / "tools.json").read_text())
    check("tools.json lists 13 tools, 6 of them write", len(cat["tools"]) == 13 and sum(t["write"] for t in cat["tools"]) == 6, cat)

    check("no settings: 7 read tools", len(await names()) == 7, await names())
    put(enabled=True, write=True)
    check("write on: 13 tools", len(await names()) == 13, await names())
    put(enabled=True, write=False, disabled_tools=["mail_read"])
    check("mail_read off: 6 tools, not mail_read", "mail_read" not in await names() and len(await names()) == 6, await names())
    r = await call("mail_read", uid=1)
    check("mail_read refused", "turned off" in r, r)
    r = await call("mail_move", uids=[1], to_folder="Trash")
    check("write tool refused while write is off", "write access is off" in r, r)
    r = await call("mail_list_identities")
    check("an allowed call runs (fails here: no Roundcube DB)", r.startswith("raised") and "turned off" not in r, r)
    put(enabled=False)
    check("MCP off: no tool listed", await names() == [], await names())
    r = await call("contacts_search", query="x" * 500)
    check("MCP off: calls refused", "turned off" in r, r)

    log = calls()
    check("4 calls recorded", len(log) == 4, log)
    check("refusals marked", [bool(e.get("refused")) for e in log] == [True, True, False, True], log)
    check("arguments shortened", len(log[-1]["args"]["query"]) <= 121, log[-1]["args"])
    check("each has time, tool, ms, ok", all({"ts", "tool", "ms", "ok"} <= e.keys() for e in log), log)

    put(enabled=False, cleared_at="2999-01-01T00:00:00+00:00")
    await call("mail_read", uid=1)
    check("cleared: only the call made after", len(calls()) == 1, calls())


asyncio.run(main())
sys.exit(1 if failures else 0)
