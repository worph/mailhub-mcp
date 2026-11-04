"""CI smoke test: the server imports, and registers 7 tools read-only / 13 with writes."""
import asyncio, importlib.util, os, pathlib, sys

expected = 13 if os.environ.get("MAILHUB_MCP_WRITE") == "true" else 7
spec = importlib.util.spec_from_file_location("server", pathlib.Path(__file__).with_name("server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
names = sorted(t.name for t in asyncio.run(mod.server.list_tools()))
print(len(names), names)
sys.exit(0 if len(names) == expected else f"expected {expected} tools, got {len(names)}")
