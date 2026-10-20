"""Organizer console — the human side of SPX.

A district organizer needs four things and nothing else: see who is in the
pool, see what happened, reverse a bad ban, and cancel a date. This is a
localhost-only, token-gated console built on the standard library, so it adds
no dependency and no supply chain to a self-hosted instance.

    SPX_ORG_TOKEN=... python3 -m spx.org --db spx.db --port 8787

Security posture:
  * binds 127.0.0.1 unless --host is passed explicitly
  * every request needs the token, delivered as a query parameter once and
    then stored in an HttpOnly, SameSite=Strict cookie
  * every action writes an `audit` row with the organizer's note
  * no free-text profile data is rendered because none exists
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import os
import secrets
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from spx.domain import Protocol, ProtocolError, utcnow

COOKIE = "spx_org"


class Organizer:
    """The organizer's actions, independent of HTTP."""

    def __init__(self, proto: Protocol):
        self.p = proto

    # -- reads ------------------------------------------------------------
    def dashboard(self) -> dict:
        q = self.p.conn.execute
        now = utcnow()
        statuses = {r["status"]: r["n"] for r in q(
            "SELECT status, COUNT(*) n FROM members GROUP BY status")}
        return {
            "members": statuses,
            "bans": q("SELECT COUNT(*) n FROM retained_markers "
                      "WHERE kind='banned' AND expires_at > ?", (now,)).fetchone()["n"],
            "confirmed": q("SELECT COUNT(*) n FROM dates "
                           "WHERE outcome='CONFIRMED' AND window_start > ?",
                           (now,)).fetchone()["n"],
            "completed": q("SELECT COUNT(*) n FROM dates "
                           "WHERE meeting_status='COMPLETED'").fetchone()["n"],
            "pending": q("SELECT COUNT(*) n FROM proposals "
                         "WHERE decision_a IS NULL OR decision_b IS NULL").fetchone()["n"],
            "metrics": self.p.metrics(),
        }

    def members(self, limit: int = 200) -> list[dict]:
        rows = self.p.conn.execute(
            """SELECT member_id, district, age, status, phone_hash, created_at
                 FROM members ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def audit(self, limit: int = 100) -> list[dict]:
        rows = self.p.conn.execute(
            "SELECT at, actor, action, subject, detail FROM audit "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def dates(self, limit: int = 50) -> list[dict]:
        rows = self.p.conn.execute(
            """SELECT d.date_id, d.outcome, d.window_start, d.meeting_status,
                      v.name AS venue
                 FROM dates d LEFT JOIN venues v ON v.venue_id = d.venue_id
                ORDER BY d.rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- writes -----------------------------------------------------------
    def reinstate(self, member_id: str, note: str) -> None:
        """Undo a ban. This is a human judgment, so it cannot be silent."""
        if not note.strip():
            raise ProtocolError("a reinstate requires a note")
        row = self.p._member(member_id)
        if row["status"] != "banned":
            raise ProtocolError("member is not banned")

        # The ban marker is what would block a rejoin; it must go with the ban.
        marker = row["phone_hash"] or hashlib.sha256(
            f"spx-marker:{member_id}".encode()).hexdigest()
        self.p.conn.execute(
            "DELETE FROM retained_markers WHERE marker_hash=? AND kind='banned'",
            (marker,))
        self.p.conn.execute(
            "UPDATE members SET status='active' WHERE member_id=?", (member_id,))
        self.p.conn.execute(
            "INSERT INTO audit (at, actor, action, subject, detail) "
            "VALUES (?,?,?,?,?)",
            (utcnow(), "organizer", "reinstate", member_id, note.strip()))
        self.p.conn.commit()

    def cancel_date(self, date_id: str, note: str) -> None:
        """Cancel a confirmed date, release the slot, void open pledges."""
        if not note.strip():
            raise ProtocolError("a cancellation requires a note")
        row = self.p._date(date_id)
        if row["outcome"] != "CONFIRMED":
            raise ProtocolError("only a confirmed date can be cancelled")

        self.p.conn.execute(
            "UPDATE dates SET outcome='LAPSED', lapse_cause='ORGANIZER_CANCELLED' "
            "WHERE date_id=?", (date_id,))
        # A cancellation is not a broken pledge; nobody is punished for it.
        self.p.conn.execute(
            "UPDATE date_pledges SET status='VOID', resolved_at=? "
            "WHERE date_id=? AND status='PLEDGED'", (utcnow(), date_id))
        self.p.conn.execute(
            "INSERT INTO audit (at, actor, action, subject, detail) "
            "VALUES (?,?,?,?,?)",
            (utcnow(), "organizer", "cancel_date", date_id, note.strip()))
        self.p.conn.commit()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0b0b0d; color:#e6e6e6; font:15px/1.5 ui-monospace,Menlo,monospace;
         margin:0; padding:24px; }}
  h1 {{ font-size:18px; letter-spacing:.08em; color:#ff8a70; margin:0 0 16px; }}
  h2 {{ font-size:13px; letter-spacing:.12em; color:#8a8a8a; margin:28px 0 8px; }}
  a {{ color:#ffb340; }}
  table {{ border-collapse:collapse; width:100%; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #222; }}
  th {{ color:#8a8a8a; font-weight:normal; font-size:12px; letter-spacing:.08em; }}
  .tag {{ padding:1px 6px; border:1px solid #333; border-radius:3px; font-size:12px; }}
  .banned {{ color:#ff5b4a; border-color:#ff5b4a; }}
  .active {{ color:#6fe08a; border-color:#6fe08a; }}
  .banned-tag {{ color:#ff5b4a; }}
  form {{ display:inline; }}
  input[type=text] {{ background:#111; border:1px solid #333; color:#eee; padding:4px 6px; }}
  button {{ background:#1a1a1f; color:#ffb340; border:1px solid #444; padding:4px 10px;
            cursor:pointer; }}
  .muted {{ color:#777; }}
  .nav a {{ margin-right:16px; }}
</style></head><body>
<div class="nav"><a href="/">dashboard</a><a href="/members">members</a>
<a href="/dates">dates</a><a href="/audit">audit</a></div>
<h1>{html.escape(title)}</h1>
{body}
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "spx-org/0.1"

    # -- auth -------------------------------------------------------------
    def _token_ok(self, query_token: str | None = None) -> bool:
        if query_token and secrets.compare_digest(query_token, self.server.token):
            return True
        raw = self.headers.get("Cookie", "")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return False
        got = jar[COOKIE].value if COOKIE in jar else ""
        return bool(got) and secrets.compare_digest(got, self.server.token)

    def _send(self, code: int, body: str, headers: dict | None = None) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _deny(self) -> None:
        self._send(401, _page("locked", "<p>Token required. Open the console URL "
                                      "printed at startup.</p>"))

    def _csrf(self) -> str:
        """Synchronizer token, derived from the session secret.

        The page carries it; another origin cannot read the page, so it cannot
        forge a POST. Derived rather than stored, so a restart invalidates it
        along with the cookie.
        """
        return hmac.new(self.server.token.encode(), b"spx-csrf",
                        hashlib.sha256).hexdigest()

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        token = qs.get("token", [None])[0]

        if token and self._token_ok(token):
            cookie = (f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_response(303)
            self.send_header("Location", url.path or "/")
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            return
        if not self._token_ok():
            return self._deny()

        org = self._open()
        try:
            if url.path == "/":
                return self._dashboard(org)
            if url.path == "/members":
                return self._members(org)
            if url.path == "/dates":
                return self._dates(org)
            if url.path == "/audit":
                return self._audit(org)
            if url.path == "/health":
                return self._send(200, "ok")
        finally:
            org.p.close()
        self._send(404, _page("not found", "<p>No such page.</p>"))

    def do_POST(self) -> None:
        if not self._token_ok():
            return self._deny()
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8192:
            return self._send(413, _page("too large", "<p>Form too large.</p>"))
        form = parse_qs(self.rfile.read(length).decode())
        got = form.get("csrf", [""])[0]
        if not hmac.compare_digest(got, self._csrf()):
            return self._send(403, _page("rejected", "<p>Missing or bad CSRF token.</p>"))

        org = self._open()
        path = urlparse(self.path).path
        note = form.get("note", [""])[0]
        try:
            if path == "/reinstate":
                org.reinstate(form.get("member_id", [""])[0], note)
                return self._redirect("/members")
            if path == "/cancel_date":
                org.cancel_date(form.get("date_id", [""])[0], note)
                return self._redirect("/dates")
        except ProtocolError as exc:
            return self._send(400, _page("rejected", f"<p>{html.escape(str(exc))}</p>"))
        finally:
            org.p.close()
        self._send(404, _page("not found", "<p>No such action.</p>"))

    def _open(self) -> Organizer:
        """A connection per request: sqlite3 objects cannot cross threads."""
        return Organizer(Protocol.open(self.server.db_path))

    def _redirect(self, where: str) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    # -- pages ------------------------------------------------------------
    def _dashboard(self, org: Organizer) -> None:
        d = org.dashboard()
        m = d["metrics"]
        rows = "".join(
            f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
            for k, v in sorted(d["members"].items()))
        body = f"""
<h2>pool</h2>
<table><tr><th>status</th><th>count</th></tr>{rows or '<tr><td colspan=2>empty</td></tr>'}</table>
<h2>activity</h2>
<table>
<tr><th>open proposals</th><td>{d['pending']}</td></tr>
<tr><th>confirmed dates</th><td>{d['confirmed']}</td></tr>
<tr><th>completed meetings</th><td>{d['completed']}</td></tr>
<tr><th>active bans</th><td>{d['bans']}</td></tr>
</table>
<h2>rates</h2>
<table>
<tr><th>feasible pairs (f)</th><td>{m['f'] if m['f'] is not None else '—'} of {m['pairs']}</td></tr>
<tr><th>mutual accept (p)</th><td>{m['p'] if m['p'] is not None else '—'} of {m['proposals_resolved']}</td></tr>
<tr><th>mutual reveals</th><td>{m['mutual_reveals']} of {m['completed']} completed</td></tr>
</table>"""
        self._send(200, _page("SPX organizer", body))

    def _members(self, org: Organizer) -> None:
        rows = []
        for m in org.members():
            banned = m["status"] == "banned"
            action = ""
            if banned:
                action = (
                    f"<form method=post action=/reinstate>"
                    f"<input type=hidden name=csrf value='{self._csrf()}'>"
                    f"<input type=hidden name=member_id value='{html.escape(m['member_id'])}'>"
                    f"<input type=text name=note placeholder='why' size=12 required> "
                    f"<button>reinstate</button></form>")
            cls = "banned" if banned else "active" if m["status"] == "active" else ""
            rows.append(
                f"<tr><td>{html.escape(m['member_id'][:8])}</td>"
                f"<td>{html.escape(m['district'])}</td><td>{m['age']}</td>"
                f"<td><span class='tag {cls}'>{html.escape(m['status'])}</span></td>"
                f"<td class=muted>{(m['phone_hash'] or '—')[:12]}</td>"
                f"<td>{action}</td></tr>")
        body = ("<table><tr><th>id</th><th>district</th><th>age</th>"
                "<th>status</th><th>phone hash</th><th></th></tr>"
                + "".join(rows) + "</table>")
        self._send(200, _page("members", body))

    def _dates(self, org: Organizer) -> None:
        rows = []
        for d in org.dates():
            action = ""
            if d["outcome"] == "CONFIRMED":
                action = (
                    f"<form method=post action=/cancel_date>"
                    f"<input type=hidden name=csrf value='{self._csrf()}'>"
                    f"<input type=hidden name=date_id value='{html.escape(d['date_id'])}'>"
                    f"<input type=text name=note placeholder='why' size=12 required> "
                    f"<button>cancel</button></form>")
            rows.append(
                f"<tr><td>{html.escape(d['date_id'][:8])}</td>"
                f"<td>{html.escape(d['venue'] or '—')}</td>"
                f"<td>{html.escape(d['window_start'] or '—')}</td>"
                f"<td>{html.escape(d['outcome'])}</td>"
                f"<td>{html.escape(d['meeting_status'] or '—')}</td>"
                f"<td>{action}</td></tr>")
        body = ("<table><tr><th>id</th><th>venue</th><th>window</th><th>outcome</th>"
                "<th>meeting</th><th></th></tr>" + "".join(rows) + "</table>")
        self._send(200, _page("dates", body))

    def _audit(self, org: Organizer) -> None:
        rows = "".join(
            f"<tr><td class=muted>{html.escape(r['at'])}</td>"
            f"<td>{html.escape(r['action'])}</td>"
            f"<td>{html.escape((r['subject'] or '—')[:12])}</td>"
            f"<td>{html.escape(r['detail'] or '')}</td></tr>"
            for r in org.audit())
        body = ("<table><tr><th>at</th><th>action</th><th>subject</th>"
                "<th>note</th></tr>" + rows + "</table>")
        self._send(200, _page("audit", body))

    def log_message(self, fmt: str, *args) -> None:  # quieter default
        sys.stderr.write("org: " + (fmt % args) + "\n")


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def make_server(db_path: str, token: str, host: str = "127.0.0.1",
                port: int = 8787, allow_remote: bool = False) -> ThreadingHTTPServer:
    if host not in LOOPBACK and not allow_remote:
        raise ValueError(
            "refusing to bind a non-loopback address; pass --allow-remote"
            " if you have put this behind an authenticated tunnel")
    # Create the schema once; handlers open their own short-lived connections.
    Protocol.open(db_path).close()
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path              # type: ignore[attr-defined]
    server.token = token                  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spx.org", description="SPX organizer console")
    ap.add_argument("--db", default="spx.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback bind (put it behind a tunnel)")
    args = ap.parse_args(argv)

    token = os.environ.get("SPX_ORG_TOKEN") or secrets.token_urlsafe(24)
    server = make_server(args.db, token, args.host, args.port,
                         allow_remote=args.allow_remote)
    host, port = server.server_address[:2]
    print(f"SPX organizer  http://{host}:{port}/?token={token}")
    print("token is shown once; set SPX_ORG_TOKEN to reuse it")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
