"""MailHub MCP server: the unified mailbox, calendar and contacts, for AI assistants.

Talks to MailHub's own services on the app's private network:
  - mail      IMAP on Dovecot (the unified mailbox)
  - calendar  CalDAV on the built-in Baïkal
  - contacts  CardDAV on the built-in Baïkal
  - identities (the addresses you send from) read from Roundcube's DB, read-only

Read-only unless write access is on. Even then nothing is ever sent: the
assistant writes a draft, and you send it from the webmail, where each identity
goes out through its own provider's SMTP.

Controlled from the webmail (Settings → AI assistant), through two folders:
  - $MAILHUB_MCP_SETTINGS (settings.json, written by the webmail, read here on
    every request): MCP on/off, write access, tools switched off, history cleared.
    Without it: on, write access from MAILHUB_MCP_WRITE, every tool.
  - $MAILHUB_MCP_LOG_DIR (written here, read by the webmail): tools.json, the
    catalog the settings page lists; calls.jsonl, one line per tool call (time,
    tool, shortened arguments, duration, outcome; never the result).
"""
from __future__ import annotations

import datetime as dt
import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import httpx
import icalendar
import vobject
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.server import Context
from mcp.types import CallToolResult, Tool as MCPTool
from mcp.server.mcpserver.exceptions import ToolError  # message reaches the assistant
from mcp.server.transport_security import TransportSecuritySettings

USER = os.environ.get("MAILHUB_USER", "admin")
PASSWORD = os.environ["MAILHUB_PASSWORD"]
IMAP_HOST = os.environ.get("MAILHUB_IMAP_HOST", "mailhub-imap")
IMAP_PORT = int(os.environ.get("MAILHUB_IMAP_PORT", "31143"))
DAV_URL = os.environ.get("MAILHUB_DAV_URL", "http://mailhub-dav/dav.php").rstrip("/")
ROUNDCUBE_DB = os.environ.get("MAILHUB_ROUNDCUBE_DB", "/roundcube/sqlite.db")
WRITE = os.environ.get("MAILHUB_MCP_WRITE", "false").lower() in ("1", "true", "yes")
SETTINGS_FILE = os.path.join(os.environ.get("MAILHUB_MCP_SETTINGS", "/settings"), "settings.json")
LOG_DIR = os.environ.get("MAILHUB_MCP_LOG_DIR", "/log")
HISTORY_DAYS = int(os.environ.get("MAILHUB_MCP_HISTORY_DAYS", "30"))
HISTORY_MAX = int(os.environ.get("MAILHUB_MCP_HISTORY_MAX", "5000"))
try:  # the platform's $TZ is not always a valid zone name
    TZ = ZoneInfo(os.environ.get("MAILHUB_TZ") or "Etc/UTC")
except Exception:
    TZ = ZoneInfo("Etc/UTC")

WRITE_TOOLS: set[str] = set()


# ----------------------------------------------------------------- settings ---
_settings_cache: tuple[float, dict[str, Any]] | None = None


def settings() -> dict[str, Any]:
    """The webmail's settings, re-read when the file changes. A missing or broken
    file means the defaults: on, write access as MAILHUB_MCP_WRITE, every tool."""
    global _settings_cache
    out: dict[str, Any] = {"enabled": True, "write": WRITE, "disabled_tools": [], "cleared_at": None}
    try:
        mtime = os.stat(SETTINGS_FILE).st_mtime
    except OSError:
        return out
    if _settings_cache is None or _settings_cache[0] != mtime:
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            data = {}
        _settings_cache = (mtime, data)
    data = _settings_cache[1]
    if isinstance(data.get("enabled"), bool):
        out["enabled"] = data["enabled"]
    if isinstance(data.get("write"), bool):
        out["write"] = data["write"]
    if isinstance(data.get("disabled_tools"), list):
        out["disabled_tools"] = [t for t in data["disabled_tools"] if isinstance(t, str)]
    if isinstance(data.get("cleared_at"), str):
        out["cleared_at"] = data["cleared_at"]
    return out


def refusal(name: str, conf: dict[str, Any]) -> str | None:
    """Why `name` may not be called now, or None."""
    if not conf["enabled"]:
        return "MailHub's MCP access is turned off (MailHub webmail: Settings → AI assistant)."
    if name in WRITE_TOOLS and not conf["write"]:
        return (f"{name} changes mail, calendar or contacts, and write access is off "
                "(MailHub webmail: Settings → AI assistant).")
    if name in conf["disabled_tools"]:
        return f"{name} is turned off (MailHub webmail: Settings → AI assistant)."
    return None


# ------------------------------------------------------------------ history ---
class History:
    """calls.jsonl: one JSON object per call, appended; rewritten (temp file and
    rename, so a reader never sees half of it) to drop calls older than
    HISTORY_DAYS, beyond HISTORY_MAX, or from before the history was cleared."""

    def __init__(self, folder: str):
        self.path = os.path.join(folder, "calls.jsonl")
        self.lock = threading.Lock()
        self.lines: int | None = None  # counted on first use
        self.cleared_at: str | None = None

    def _usable(self) -> bool:
        return os.path.isdir(os.path.dirname(self.path)) and os.access(os.path.dirname(self.path), os.W_OK)

    def _prune(self, cleared_at: str | None) -> None:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds")
        if cleared_at and cleared_at > cutoff:
            cutoff = cleared_at
        kept = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        if json.loads(line).get("ts", "") >= cutoff:
                            kept.append(line if line.endswith("\n") else line + "\n")
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass
        kept = kept[-HISTORY_MAX:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)
        self.lines = len(kept)

    def add(self, entry: dict[str, Any], cleared_at: str | None) -> None:
        if not self._usable():
            return
        with self.lock:
            try:
                if self.lines is None or cleared_at != self.cleared_at:
                    self._prune(cleared_at)
                    self.cleared_at = cleared_at
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self.lines += 1
                if self.lines > HISTORY_MAX * 1.2:
                    self._prune(cleared_at)
            except OSError:
                pass  # the history never breaks a call


def _short(value: Any, limit: int = 120) -> Any:
    """Arguments as recorded: strings cut, lists cut to their first items."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:10]] + (["…"] if len(value) > 10 else [])
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    return value


class MailHubServer(MCPServer):
    """Lists only the tools the settings allow, refuses the others (a client may
    still hold an older list), and records every call."""

    async def list_tools(self) -> list[MCPTool]:
        conf = settings()
        return [t for t in await super().list_tools() if refusal(t.name, conf) is None]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None):
        conf = settings()
        entry: dict[str, Any] = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                 "tool": name, "args": _short(arguments)}
        started = time.monotonic()
        try:
            reason = refusal(name, conf)
            if reason:
                entry["refused"] = True
                raise ToolError(reason)
            result = await super().call_tool(name, arguments, context)
            if isinstance(result, CallToolResult) and result.is_error:
                entry["ok"] = False
                entry["error"] = " ".join(getattr(c, "text", "") for c in result.content)[:300]
            else:
                entry["ok"] = True
            return result
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:300]
            raise
        finally:
            entry["ms"] = round((time.monotonic() - started) * 1000)
            history.add(entry, conf["cleared_at"])


history = History(LOG_DIR)

server = MailHubServer(
    name="mailhub",
    instructions=(
        "MailHub: the user's unified mailbox (every fetched account in one INBOX), "
        "calendar and contacts. Search before reading; read by uid+folder. "
        "Tools that change mail, calendar or contacts are listed only while the user "
        "allows it (MailHub webmail: Settings → AI assistant); drafts are saved to "
        "Drafts and never sent — the user sends them from the webmail."
    ),
)


def write_tool(fn):
    """A tool that changes mail, calendar or contacts: listed and callable only while
    write access is on."""
    WRITE_TOOLS.add(fn.__name__)
    return server.tool()(fn)


def write_catalog() -> None:
    """tools.json, for the settings page: every tool, whether it writes, its summary."""
    if not os.access(LOG_DIR, os.W_OK):
        return
    tools = []
    for t in server._tool_manager.list_tools():
        tools.append({"name": t.name, "write": t.name in WRITE_TOOLS,
                      "description": (t.description or "").strip().split("\n")[0]})
    path = os.path.join(LOG_DIR, "tools.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"tools": tools, "write_default": WRITE}, f, ensure_ascii=False, indent=1)
    os.chmod(path + ".tmp", 0o644)
    os.replace(path + ".tmp", path)


# --------------------------------------------------------------------- IMAP ---

def _q(s: str) -> str:
    """Quote a string for an IMAP command."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Imap:
    def __enter__(self) -> imaplib.IMAP4:
        self.m = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=30)
        self.m.login(USER, PASSWORD)
        try:
            self.m.enable("UTF8=ACCEPT")  # UTF-8 folder names and search terms
        except imaplib.IMAP4.error:
            pass
        return self.m

    def __exit__(self, *exc):
        try:
            self.m.logout()
        except Exception:
            pass


def _select(m: imaplib.IMAP4, folder: str, readonly: bool = True) -> None:
    typ, data = m.select(_q(folder), readonly=readonly)
    if typ != "OK":
        raise ToolError(f"no such folder: {folder}")


def _imap_date(day: str) -> str:
    return dt.date.fromisoformat(day).strftime("%d-%b-%Y")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


def _addresses(value: str | None) -> list[str]:
    return [email.utils.formataddr((_decode(n), a)) if n else a
            for n, a in email.utils.getaddresses([value or ""]) if a]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _html_to_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    text = html.unescape("".join(p.out))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _summary(uid: str, folder: str, flags: str, header_bytes: bytes,
             internal: str = "") -> dict[str, Any]:
    h = email.message_from_bytes(header_bytes, policy=email.policy.default)
    date = h.get("Date", "")
    if not date and internal:  # no Date header: when the message arrived instead
        parsed = imaplib.Internaldate2tuple(f'INTERNALDATE "{internal}"'.encode())
        date = email.utils.formatdate(dt.datetime(*parsed[:6]).timestamp(), localtime=False) if parsed else ""
    return {
        "uid": int(uid),
        "folder": folder,
        "date": date,
        "from": _addresses(h.get("From")),
        "to": _addresses(h.get("To")),
        "subject": _decode(h.get("Subject")),
        "seen": "\\Seen" in flags,
        "flagged": "\\Flagged" in flags,
    }


def _fetch_summaries(m: imaplib.IMAP4, folder: str, uids: list[bytes]) -> list[dict[str, Any]]:
    if not uids:
        return []
    typ, data = m.uid("FETCH", b",".join(uids).decode(),
                      "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    by_uid = {}
    for item in data:
        if isinstance(item, tuple):
            meta = item[0].decode()
            uid = re.search(r"UID (\d+)", meta).group(1)
            flags = (re.search(r"FLAGS \(([^)]*)\)", meta) or [None, ""])[1]
            internal = (re.search(r'INTERNALDATE "([^"]+)"', meta) or [None, ""])[1]
            by_uid[uid] = _summary(uid, folder, flags, item[1], internal)
    return [by_uid[u.decode()] for u in uids if u.decode() in by_uid]


@server.tool()
def mail_list_folders() -> list[dict[str, Any]]:
    """List the mailbox folders with their message and unread counts."""
    out = []
    with _Imap() as m:
        typ, data = m.list()
        for line in data:
            line = line.decode() if isinstance(line, bytes) else line
            match = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
            if not match or "\\Noselect" in match["flags"]:
                continue
            name = match["name"].strip('"')
            typ, st = m.status(_q(name), "(MESSAGES UNSEEN)")
            counts = dict(re.findall(r"(MESSAGES|UNSEEN) (\d+)", st[0].decode()))
            out.append({"folder": name, "messages": int(counts.get("MESSAGES", 0)),
                        "unread": int(counts.get("UNSEEN", 0))})
    return out


@server.tool()
def mail_search(folder: str = "INBOX", text: str | None = None, from_: str | None = None,
                to: str | None = None, subject: str | None = None, since: str | None = None,
                before: str | None = None, unread_only: bool = False, flagged_only: bool = False,
                limit: int = 20) -> list[dict[str, Any]]:
    """Search one folder, newest first. Dates are YYYY-MM-DD (since is inclusive,
    before exclusive). `text` matches headers and body. Returns uid, date, from,
    to, subject and flags; read a message with mail_read(uid, folder)."""
    criteria: list[str] = []
    for key, value in (("TEXT", text), ("FROM", from_), ("TO", to), ("SUBJECT", subject)):
        if value:
            criteria += [key, _q(value)]
    if since:
        criteria += ["SINCE", _imap_date(since)]
    if before:
        criteria += ["BEFORE", _imap_date(before)]
    if unread_only:
        criteria.append("UNSEEN")
    if flagged_only:
        criteria.append("FLAGGED")
    with _Imap() as m:
        _select(m, folder)
        typ, data = m.uid("SEARCH", *(criteria or ["ALL"]))
        uids = data[0].split()[-max(1, min(limit, 200)):][::-1]
        return _fetch_summaries(m, folder, uids)


def _invitation(part) -> dict[str, Any] | None:
    try:
        cal = icalendar.Calendar.from_ical(part.get_content())
    except Exception:
        return None
    for ev in cal.walk("VEVENT"):
        return {
            "method": str(cal.get("METHOD", "")),
            "summary": str(ev.get("SUMMARY", "")),
            "start": _ical_time(ev.get("DTSTART")),
            "end": _ical_time(ev.get("DTEND")),
            "organizer": str(ev.get("ORGANIZER", "")).replace("mailto:", ""),
            "location": str(ev.get("LOCATION", "")),
            "note": "Answer invitations from the webmail (Accept / Maybe / Decline).",
        }
    return None


@server.tool()
def mail_read(uid: int, folder: str = "INBOX", max_chars: int = 20000) -> dict[str, Any]:
    """Read one message: headers, text body (HTML converted to text), attachment
    names, and the invitation details when it carries one. Does not mark it read."""
    with _Imap() as m:
        _select(m, folder)
        typ, data = m.uid("FETCH", str(uid), "(FLAGS INTERNALDATE BODY.PEEK[])")
        raw = next((i[1] for i in data if isinstance(i, tuple)), None)
        if raw is None:
            raise ToolError(f"no message {uid} in {folder}")
        meta = data[0][0].decode()
        flags = (re.search(r"FLAGS \(([^)]*)\)", meta) or [None, ""])[1]
        internal = (re.search(r'INTERNALDATE "([^"]+)"', meta) or [None, ""])[1]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    attachments, invitation = [], None
    for part in msg.walk():
        if part.get_content_type() == "text/calendar" and invitation is None:
            invitation = _invitation(part)
        elif part.get_content_disposition() == "attachment":
            attachments.append({"name": part.get_filename() or "", "type": part.get_content_type(),
                                "size": len(part.get_payload(decode=True) or b"")})
    return {
        **_summary(str(uid), folder, flags, raw.split(b"\r\n\r\n", 1)[0], internal),
        "cc": _addresses(msg.get("Cc")),
        "reply_to": _addresses(msg.get("Reply-To")),
        "message_id": msg.get("Message-ID", ""),
        "body": body[:max_chars] + ("\n[…truncated]" if len(body) > max_chars else ""),
        "attachments": attachments,
        "invitation": invitation,
    }


@server.tool()
def mail_list_identities() -> list[dict[str, Any]]:
    """The addresses the user sends from (their webmail identities)."""
    # immutable=1: the DB is mounted read-only, where SQLite cannot take its
    # locks (mode=ro fails). Roundcube uses a rollback journal, so there is no WAL
    # to miss; a read that races a write is retried once.
    for attempt in (1, 2):
        con = sqlite3.connect(f"file:{ROUNDCUBE_DB}?immutable=1", uri=True)
        try:
            rows = con.execute("SELECT name, email, standard FROM identities WHERE del = 0 "
                               "ORDER BY standard DESC, email").fetchall()
            break
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise
        finally:
            con.close()
    # Skip Roundcube's auto-created login identity (admin@<IMAP host>): not a
    # real address, nothing to send from.
    return [{"name": n, "email": e, "default": bool(s)} for n, e, s in rows
            if "@" in (e or "") and "." in e.rsplit("@", 1)[1]]


@write_tool
def mail_set_flags(uids: list[int], folder: str = "INBOX", seen: bool | None = None,
                   flagged: bool | None = None) -> str:
    """Mark messages read/unread and/or flagged/unflagged."""
    with _Imap() as m:
        _select(m, folder, readonly=False)
        ids = ",".join(str(u) for u in uids)
        for flag, value in (("\\Seen", seen), ("\\Flagged", flagged)):
            if value is not None:
                m.uid("STORE", ids, "+FLAGS" if value else "-FLAGS", f"({flag})")
    return f"updated {len(uids)} message(s)"


@write_tool
def mail_move(uids: list[int], to_folder: str, folder: str = "INBOX") -> str:
    """Move messages to another folder (to_folder="Trash" to delete them)."""
    with _Imap() as m:
        _select(m, folder, readonly=False)
        typ, data = m.uid("MOVE", ",".join(str(u) for u in uids), _q(to_folder))
        if typ != "OK":
            raise ToolError(f"move failed: {data}")
    return f"moved {len(uids)} message(s) to {to_folder}"


@write_tool
def mail_create_draft(from_email: str, subject: str = "", body: str = "",
                      to: list[str] | None = None, cc: list[str] | None = None,
                      reply_to_uid: int | None = None, reply_folder: str = "INBOX") -> str:
    """Save a draft in Drafts — it is NOT sent; the user reviews and sends it from
    the webmail. from_email must be one of mail_list_identities(). With
    reply_to_uid, threading headers are set and to/subject default to a reply."""
    identity = next((i for i in mail_list_identities() if i["email"].lower() == from_email.lower()), None)
    if identity is None:
        raise ToolError("from_email is not one of the user's identities (see mail_list_identities)")
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((identity["name"], identity["email"]))
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=identity["email"].split("@")[1])
    with _Imap() as m:
        if reply_to_uid is not None:
            _select(m, reply_folder)
            typ, data = m.uid("FETCH", str(reply_to_uid),
                              "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT FROM REPLY-TO)])")
            raw = next((i[1] for i in data if isinstance(i, tuple)), None)
            if raw is None:
                raise ToolError(f"no message {reply_to_uid} in {reply_folder}")
            orig = email.message_from_bytes(raw, policy=email.policy.default)
            if orig.get("Message-ID"):
                msg["In-Reply-To"] = orig["Message-ID"]
                msg["References"] = (orig.get("References", "") + " " + orig["Message-ID"]).strip()
            subject = subject or ("Re: " + re.sub(r"^(re:\s*)+", "", _decode(orig.get("Subject")), flags=re.I))
            to = to or _addresses(orig.get("Reply-To") or orig.get("From"))
        msg["To"] = ", ".join(to or [])
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        typ, data = m.append("Drafts", "(\\Draft \\Seen)", imaplib.Time2Internaldate(dt.datetime.now(dt.timezone.utc)),
                             msg.as_bytes(policy=email.policy.SMTP))
        if typ != "OK":
            raise ToolError(f"could not save the draft: {data}")
    return "draft saved in Drafts — open the webmail to review and send it"


# ---------------------------------------------------------------- CalDAV ------

def _dav(method: str, path: str, body: str | None = None, depth: str | None = None,
         extra: dict[str, str] | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/xml; charset=utf-8", **(extra or {})}
    if depth is not None:
        headers["Depth"] = depth
    url = path if path.startswith("http") else DAV_URL + path
    r = httpx.request(method, url, content=body, headers=headers, auth=(USER, PASSWORD), timeout=30)
    if r.status_code >= 400:
        raise ToolError(f"DAV {method} {path}: HTTP {r.status_code}")
    return r


_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "card": "urn:ietf:params:xml:ns:carddav"}


def _responses(xml_text: str):
    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml_text)
    for resp in root.findall("d:response", _NS):
        yield resp.findtext("d:href", default="", namespaces=_NS), resp


def _ical_time(prop) -> str:
    if prop is None:
        return ""
    value = prop.dt
    if isinstance(value, dt.datetime):
        return (value.astimezone(TZ) if value.tzinfo else value).isoformat()
    return value.isoformat()


def _utc_bound(value: str) -> str:
    """YYYY-MM-DD (local midnight) or an ISO date-time -> CalDAV UTC stamp."""
    when = (dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), TZ) if len(value) == 10
            else _parse_when(value, False))
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_when(value: str, all_day: bool) -> dt.date | dt.datetime:
    if all_day:
        return dt.date.fromisoformat(value[:10])
    when = dt.datetime.fromisoformat(value)
    return when if when.tzinfo else when.replace(tzinfo=TZ)


@server.tool()
def calendar_list() -> list[dict[str, str]]:
    """List the calendars (use the id with the other calendar tools)."""
    body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            "<d:displayname/><d:resourcetype/></d:prop></d:propfind>")
    out = []
    for href, resp in _responses(_dav("PROPFIND", f"/calendars/{USER}/", body, "1").text):
        if resp.find(".//c:calendar", _NS) is not None:
            out.append({"id": href.rstrip("/").rsplit("/", 1)[-1],
                        "name": resp.findtext(".//d:displayname", default="", namespaces=_NS)})
    return out


def _calendar_query(calendar: str, inner_filter: str, expand: tuple[str, str] | None = None):
    expand_xml = f'<c:expand start="{expand[0]}" end="{expand[1]}"/>' if expand else ""
    body = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop><d:getetag/><c:calendar-data>{expand_xml}</c:calendar-data></d:prop>"
            f'<c:filter><c:comp-filter name="VCALENDAR">{inner_filter}</c:comp-filter></c:filter>'
            "</c:calendar-query>")
    r = _dav("REPORT", f"/calendars/{USER}/{calendar}/", body, "1")
    for href, resp in _responses(r.text):
        data = resp.findtext(".//c:calendar-data", default="", namespaces=_NS)
        if data:
            yield href, icalendar.Calendar.from_ical(data)


@server.tool()
def calendar_events(start: str, end: str, calendar: str | None = None) -> list[dict[str, Any]]:
    """Events between start and end (YYYY-MM-DD or ISO date-times), recurring
    events expanded, across all calendars unless one is given."""
    rng = (_utc_bound(start), _utc_bound(end))
    flt = f'<c:comp-filter name="VEVENT"><c:time-range start="{rng[0]}" end="{rng[1]}"/></c:comp-filter>'
    events = []
    for cal in ([calendar] if calendar else [c["id"] for c in calendar_list()]):
        for href, ical in _calendar_query(cal, flt, rng):
            for ev in ical.walk("VEVENT"):
                events.append({
                    "uid": str(ev.get("UID", "")),
                    "calendar": cal,
                    "summary": str(ev.get("SUMMARY", "")),
                    "start": _ical_time(ev.get("DTSTART")),
                    "end": _ical_time(ev.get("DTEND")),
                    "all_day": not isinstance(ev.get("DTSTART").dt, dt.datetime),
                    "location": str(ev.get("LOCATION", "")),
                    "description": str(ev.get("DESCRIPTION", ""))[:1000],
                    "organizer": str(ev.get("ORGANIZER", "")).replace("mailto:", ""),
                    "status": str(ev.get("STATUS", "")),
                })
    return sorted(events, key=lambda x: x["start"])


@write_tool
def calendar_create_event(summary: str, start: str, end: str, calendar: str = "default",
                          location: str = "", description: str = "", all_day: bool = False) -> dict[str, str]:
    """Create an event. start/end are ISO date-times (local time unless an offset
    is given), or YYYY-MM-DD with all_day=true (end = the day after the last day)."""
    ev = icalendar.Event()
    uid = f"{uuid.uuid4()}@mailhub"
    ev.add("uid", uid)
    ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
    ev.add("summary", summary)
    ev.add("dtstart", _parse_when(start, all_day))
    ev.add("dtend", _parse_when(end, all_day))
    if location:
        ev.add("location", location)
    if description:
        ev.add("description", description)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Yundera//MailHub MCP//EN")
    cal.add("version", "2.0")
    cal.add_component(ev)
    _dav("PUT", f"/calendars/{USER}/{calendar}/{uuid.uuid4()}.ics", cal.to_ical().decode(),
         extra={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    return {"uid": uid, "calendar": calendar}


@write_tool
def calendar_delete_event(uid: str, calendar: str = "default") -> str:
    """Delete an event by its uid (from calendar_events)."""
    flt = ('<c:comp-filter name="VEVENT"><c:prop-filter name="UID">'
           f'<c:text-match collation="i;octet">{html.escape(uid)}</c:text-match></c:prop-filter></c:comp-filter>')
    hrefs = [href for href, _ in _calendar_query(calendar, flt)]
    if not hrefs:
        raise ToolError(f"no event {uid} in calendar {calendar}")
    for href in hrefs:
        _dav("DELETE", DAV_URL.split("/dav.php")[0] + href)
    return f"deleted {uid}"


# --------------------------------------------------------------- CardDAV ------

def _cards():
    body = ('<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop><d:getetag/><card:address-data/></d:prop></card:addressbook-query>")
    r = _dav("REPORT", f"/addressbooks/{USER}/default/", body, "1")
    for href, resp in _responses(r.text):
        data = resp.findtext(".//card:address-data", default="", namespaces=_NS)
        if data:
            try:
                yield vobject.readOne(data)
            except Exception:
                continue


def _card_dict(card) -> dict[str, Any]:
    def values(name):
        return [str(c.value) for c in card.contents.get(name, [])]
    org = card.contents.get("org")
    return {"name": values("fn")[0] if values("fn") else "", "emails": values("email"),
            "phones": values("tel"), "organization": " ".join(org[0].value) if org else ""}


@server.tool()
def contacts_search(query: str = "", limit: int = 25) -> list[dict[str, Any]]:
    """Search the address book by name, email, phone or organization (empty query
    lists everyone)."""
    q = query.lower()
    out = []
    for card in _cards():
        c = _card_dict(card)
        if not q or q in " ".join([c["name"], c["organization"], *c["emails"], *c["phones"]]).lower():
            out.append(c)
    return sorted(out, key=lambda c: c["name"].lower())[:limit]


@write_tool
def contacts_create(name: str, email_address: str = "", phone: str = "", organization: str = "") -> str:
    """Add a contact to the address book."""
    card = vobject.vCard()
    card.add("uid").value = str(uuid.uuid4())
    card.add("fn").value = name
    parts = name.split(" ", 1)
    card.add("n").value = vobject.vcard.Name(family=parts[1] if len(parts) > 1 else "", given=parts[0])
    if email_address:
        e = card.add("email")
        e.value, e.type_param = email_address, "INTERNET"
    if phone:
        card.add("tel").value = phone
    if organization:
        card.add("org").value = [organization]
    _dav("PUT", f"/addressbooks/{USER}/default/{card.uid.value}.vcf", card.serialize(),
         extra={"Content-Type": "text/vcard; charset=utf-8", "If-None-Match": "*"})
    return f"added {name}"


if __name__ == "__main__":
    write_catalog()
    anyio.run(lambda: server.run_streamable_http_async(
        host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
        stateless_http=True, json_response=True,
        # Reachable only on the app's private network (beaconify and Beacon proxy
        # to it), where the Host header is a container name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    ))
