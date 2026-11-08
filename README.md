# mailhub-mcp

`ghcr.io/worph/mailhub-mcp`: the MCP server of the Yundera store's **MailHub** app, over the unified mailbox
(IMAP), calendar (CalDAV) and contacts (CardDAV). Read-only unless `MAILHUB_MCP_WRITE=true`; never sends mail.

The app itself lives in the store: `Yundera/AppStore` → `Apps/MailHub`, where a beaconify sidecar announces it
to Beacon. The test kit (`dev/mcp-test.py` calls the tools) is in [worph/mailhub](https://github.com/worph/mailhub),
where this image was built up to 0.4.1 (history kept here).

Settings: `MAILHUB_PASSWORD` (required), `MAILHUB_USER` (`admin`), `MAILHUB_IMAP_HOST` / `_PORT`
(`mailhub-imap:31143`), `MAILHUB_DAV_URL` (`http://mailhub-dav/dav.php`), `MAILHUB_ROUNDCUBE_DB`
(`/roundcube/sqlite.db`, read for the sending identities), `MAILHUB_TZ`, `MAILHUB_MCP_WRITE`, `PORT` (8080).
Serves stateless streamable HTTP on `/mcp`.

`python check_tools.py` checks that it registers 7 tools (13 with `MAILHUB_MCP_WRITE=true`).

## Building and releasing

```sh
docker build -t mailhub-mcp .
```

CI (`.github/workflows/build.yml`) builds amd64 + arm64 on every push and PR, and checks the tool count. Pushing a tag `vX.Y.Z`
publishes `ghcr.io/worph/mailhub-mcp:X.Y.Z` (and `X.Y`); then bump its tag in the store's
`Apps/MailHub/docker-compose.yml`. Versions continue from worph/mailhub: 0.4.1 was the last one built there.
