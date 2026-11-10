# mailhub-mcp

`ghcr.io/worph/mailhub-mcp`: the MCP server of the Yundera store's **MailHub** app, over the unified mailbox
(IMAP), calendar (CalDAV) and contacts (CardDAV). Read-only unless write access is on; never sends mail.

The app itself lives in the store: `Yundera/AppStore` → `Apps/MailHub`, where a beaconify sidecar announces it
to Beacon. The test kit (`dev/mcp-test.py` calls the tools) is in [worph/mailhub](https://github.com/worph/mailhub),
where this image was built up to 0.4.1 (history kept here).

Settings: `MAILHUB_PASSWORD` (required), `MAILHUB_USER` (`admin`), `MAILHUB_IMAP_HOST` / `_PORT`
(`mailhub-imap:31143`), `MAILHUB_DAV_URL` (`http://mailhub-dav/dav.php`), `MAILHUB_ROUNDCUBE_DB`
(`/roundcube/sqlite.db`, read for the sending identities), `MAILHUB_TZ`, `MAILHUB_MCP_WRITE` (write access
until the webmail sets it), `MAILHUB_MCP_SETTINGS` (`/settings`), `MAILHUB_MCP_LOG_DIR` (`/log`),
`MAILHUB_MCP_HISTORY_DAYS` (30), `MAILHUB_MCP_HISTORY_MAX` (5000 calls), `PORT` (8080).
Serves stateless streamable HTTP on `/mcp`.

**Controlled from the webmail** (mailhub-roundcube's `mailhub_mcp` plugin, *Settings → AI assistant*), through two
folders, each written by one side only:
- `$MAILHUB_MCP_SETTINGS/settings.json`, written by the webmail, read on every request:
  `{"enabled": true, "write": false, "disabled_tools": ["…"], "cleared_at": "<ISO time>"}`. Without it: on, write
  access as `MAILHUB_MCP_WRITE`, every tool. `tools/list` shows only the tools allowed (none when off; beaconify
  re-reads it periodically), and a call to any other is refused with a message naming the setting, for clients
  still holding an older list.
- `$MAILHUB_MCP_LOG_DIR`, written here: `tools.json` (every tool, whether it writes, its summary: what the settings
  page lists) and `calls.jsonl`, one line per call: `ts` (UTC), `tool`, `args` (strings cut to 120 characters,
  lists to 10 items), `ms`, `ok`, `error`, `refused`. Results are never recorded. Kept for
  `MAILHUB_MCP_HISTORY_DAYS`, at most `MAILHUB_MCP_HISTORY_MAX` calls, and only after `cleared_at`; rewritten by
  rename, so a reader never sees a half file. Without a writable folder, nothing is recorded.

The caller is not recorded: Beacon reaches the server through beaconify, which passes nothing about it.

`python check_tools.py` checks that it registers 7 tools (13 with `MAILHUB_MCP_WRITE=true`); `python
check_settings.py` checks the settings (tools listed and refused) and the history.

## Building and releasing

```sh
docker build -t mailhub-mcp .
```

CI (`.github/workflows/build.yml`) builds amd64 + arm64 on every push and PR, and checks the tool count. Pushing a tag `vX.Y.Z`
publishes `ghcr.io/worph/mailhub-mcp:X.Y.Z` (and `X.Y`); then bump its tag in the store's
`Apps/MailHub/docker-compose.yml`. Versions continue from worph/mailhub: 0.4.1 was the last one built there.
