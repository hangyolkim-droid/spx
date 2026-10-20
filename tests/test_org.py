"""Organizer actions and the localhost console."""

import hashlib
import hmac
import http.cookiejar
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from spx.domain import Protocol, ProtocolError
from spx.org import Organizer, make_server

T0 = "2026-11-01T12:00:00+00:00"
WINDOW = "2026-11-05T19:00:00+00:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.p = Protocol.open()
        self.org = Organizer(self.p)
        self.venue = self.p.add_venue("SM", "Cafe", "1 Main St")
        self.a = self._member("F", "M", "+13105550100")
        self.b = self._member("M", "F", "+13105550101")
        pid = self.p.serve_pair(self.a, self.b, at=T0)
        self.p.decide(pid, "A", "ACCEPT", at=T0)
        self.p.decide(pid, "B", "ACCEPT", at=T0)
        self.did = self.p.open_date(pid, at=T0)
        self.p.agree_venue(self.did, 1, [self.venue], [self.venue], [self.venue],
                           window_start=WINDOW, at=T0)

    def _member(self, gender, target, phone=None):
        return self.p.add_member(
            district="SM", gender=gender, target_gender=target, age=31,
            target_age_min=25, target_age_max=40, intent="long_term",
            kids="dont_want", slot_bitmask=0b1, phone=phone, at=T0)

    def ban_a(self):
        side = "A" if self.p._member_for_side(self.did, "A") == self.a else "B"
        other = "B" if side == "A" else "A"
        self.p.pledge(self.did, side, 2000, at=T0)
        return self.p.report_broken(self.did, other, at="2026-11-05T20:00:00+00:00")


class TestReinstate(Base):
    def test_reinstate_clears_ban_and_marker(self):
        banned = self.ban_a()
        self.org.reinstate(banned, "checked the receipt, charge went through")
        row = self.p.conn.execute(
            "SELECT status, phone_hash FROM members WHERE member_id=?",
            (banned,)).fetchone()
        self.assertEqual(row["status"], "active")
        marker = self.p.conn.execute(
            "SELECT 1 FROM retained_markers WHERE marker_hash=? AND kind='banned'",
            (row["phone_hash"],)).fetchone()
        self.assertIsNone(marker)

        entry = self.p.conn.execute(
            "SELECT action, detail FROM audit WHERE action='reinstate'").fetchone()
        self.assertEqual(entry["action"], "reinstate")
        self.assertIn("receipt", entry["detail"])

    def test_reinstate_requires_note(self):
        banned = self.ban_a()
        with self.assertRaises(ProtocolError):
            self.org.reinstate(banned, "   ")

    def test_reinstate_rejects_active_member(self):
        with self.assertRaises(ProtocolError):
            self.org.reinstate(self.a, "no reason")

    def test_reinstated_member_can_rejoin(self):
        banned = self.ban_a()
        self.org.reinstate(banned, "mistaken report")
        self.assertFalse(self.p.is_banned_phone("+13105550100"))


class TestCancelDate(Base):
    def test_cancel_voids_pledges_and_frees_slot(self):
        side = "A" if self.p._member_for_side(self.did, "A") == self.a else "B"
        self.p.pledge(self.did, side, 2000, at=T0)
        self.org.cancel_date(self.did, "venue flooded")

        row = self.p._date(self.did)
        self.assertEqual(row["outcome"], "LAPSED")
        self.assertEqual(row["lapse_cause"], "ORGANIZER_CANCELLED")

        pledge = self.p.conn.execute(
            "SELECT status FROM date_pledges WHERE date_id=?", (self.did,)).fetchone()
        self.assertEqual(pledge["status"], "VOID")

        # The partial unique index only covers CONFIRMED rows, so the slot is free.
        other = self.p.add_venue("SM", "Cafe", "1 Main St")
        self.assertTrue(other)

    def test_cancel_requires_confirmed_date(self):
        pid = self.p.serve_pair(self.a, self.b, at=T0)
        with self.assertRaises(ProtocolError):
            self.org.cancel_date(self.did, "")  # blank note first
        # A negotiating date cannot be cancelled.
        self.p.decide(pid, "A", "ACCEPT", at=T0)
        self.p.decide(pid, "B", "ACCEPT", at=T0)
        open_did = self.p.open_date(pid, at=T0)
        with self.assertRaises(ProtocolError):
            self.org.cancel_date(open_did, "not yet confirmed")


class TestConsole(Base):
    TOK = "secret-token"

    def _serve(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = make_server(f"{tmp.name}/spx.db", self.TOK, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def _opener(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    def _login(self, port):
        opener = self._opener()
        opener.open(f"http://127.0.0.1:{port}/?token={self.TOK}", timeout=5).read()
        return opener

    def test_requires_token(self):
        port = self._serve()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_dashboard_renders_with_cookie(self):
        port = self._serve()
        body = self._login(port).open(
            f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        self.assertIn("SPX organizer", body)
        self.assertIn("feasible pairs", body)

    def _post(self, opener, port, path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, method="POST")
        return opener.open(req, timeout=5)

    def test_post_without_csrf_is_rejected(self):
        port = self._serve()
        opener = self._login(port)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(opener, port, "/cancel_date",
                       {"date_id": "nope", "note": "x"})
        self.assertEqual(ctx.exception.code, 403)

    def test_post_with_csrf_reaches_the_action(self):
        port = self._serve()
        opener = self._login(port)
        csrf = hmac.new(self.TOK.encode(), b"spx-csrf",
                        hashlib.sha256).hexdigest()
        # Accepted past the CSRF gate, then rejected by the domain for an
        # unknown date: 400, not 403.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(opener, port, "/cancel_date",
                       {"date_id": "nope", "note": "x", "csrf": csrf})
        self.assertEqual(ctx.exception.code, 400)

    def test_refuses_non_loopback_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                make_server(f"{tmp}/spx.db", self.TOK, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
