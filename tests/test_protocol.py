"""Protocol invariants. stdlib unittest only — no dev dependencies."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from spx.domain import BookingConflict, Protocol, ProtocolError

T0 = "2026-11-01T12:00:00+00:00"
WINDOW = "2026-11-05T19:00:00+00:00"


def plus(hours: float, base: str = T0) -> str:
    dt = datetime.fromisoformat(base) + timedelta(hours=hours)
    return dt.isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.p = Protocol.open()  # in-memory
        self.a = self.member(age=30, lo=25, hi=35, slots=0b1,
                             gender="F", target="M")
        self.b = self.member(age=32, lo=25, hi=38, slots=0b1,
                             gender="M", target="F")
        self.venue = self.p.add_venue("SM", "Cafe", "1 Main St")

    def member(self, age, lo, hi, slots, gender="F", target="M",
               intent="long_term", kids="dont_want", district="SM"):
        return self.p.add_member(
            district=district, gender=gender, target_gender=target,
            age=age, target_age_min=lo, target_age_max=hi,
            intent=intent, kids=kids, slot_bitmask=slots, at=T0)

    def confirmed(self):
        pair = self.p.evaluate_pair(self.a, self.b, at=T0)
        pid = self.p.serve_pair(self.a, self.b, at=T0)
        self.p.decide(pid, "A", "ACCEPT", at=T0)
        self.p.decide(pid, "B", "ACCEPT", at=T0)
        did = self.p.open_date(pid, at=T0)
        self.p.agree_venue(did, 1, [self.venue], [self.venue], [self.venue],
                           window_start=WINDOW, at=T0)
        return did, pair


class TestFeasibility(Base):
    def test_compatible_pair_is_feasible(self):
        pair = self.p.evaluate_pair(self.a, self.b, at=T0)
        self.assertEqual(pair["feasible"], 1)

    def test_age_outside_range_blocks(self):
        old = self.member(age=58, lo=25, hi=35, slots=0b1, gender="M", target="F")
        pair = self.p.evaluate_pair(self.a, old, at=T0)
        self.assertEqual(pair["age_ok"], 0)
        self.assertEqual(pair["feasible"], 0)

    def test_no_slot_overlap_blocks(self):
        other = self.member(age=31, lo=25, hi=35, slots=0b1000, gender="M", target="F")
        pair = self.p.evaluate_pair(self.a, other, at=T0)
        self.assertEqual(pair["slot_ok"], 0)
        self.assertEqual(pair["feasible"], 0)

    def test_infeasible_pair_cannot_be_served(self):
        old = self.member(age=58, lo=25, hi=35, slots=0b1, gender="M", target="F")
        with self.assertRaises(ProtocolError):
            self.p.serve_pair(self.a, old, at=T0)


class TestHandshake(Base):
    def setUp(self):
        super().setUp()
        self.p.evaluate_pair(self.a, self.b, at=T0)
        self.pid = self.p.serve_pair(self.a, self.b, at=T0)

    def test_pending_until_both_decide(self):
        self.p.decide(self.pid, "A", "ACCEPT", at=T0)
        self.assertEqual(self.p.resolve(self.pid, at=T0), "PENDING")

    def test_mutual_accept(self):
        self.p.decide(self.pid, "A", "ACCEPT", at=T0)
        self.p.decide(self.pid, "B", "ACCEPT", at=T0)
        self.assertEqual(self.p.resolve(self.pid, at=T0), "MUTUAL_ACCEPT")

    def test_decline_wins(self):
        self.p.decide(self.pid, "A", "ACCEPT", at=T0)
        self.p.decide(self.pid, "B", "DECLINE", at=T0)
        self.assertEqual(self.p.resolve(self.pid, at=T0), "DECLINED")

    def test_silence_expires_to_timeout(self):
        self.p.decide(self.pid, "A", "ACCEPT", at=T0)
        self.assertEqual(self.p.resolve(self.pid, at=plus(13)), "TIMEOUT")

    def test_double_decision_rejected(self):
        self.p.decide(self.pid, "A", "ACCEPT", at=T0)
        with self.assertRaises(ProtocolError):
            self.p.decide(self.pid, "A", "DECLINE", at=T0)

    def test_decision_after_deadline_rejected(self):
        with self.assertRaises(ProtocolError):
            self.p.decide(self.pid, "A", "ACCEPT", at=plus(13))


class TestVenue(Base):
    def test_confirmed_date_sets_slot(self):
        did, _ = self.confirmed()
        row = self.p._date(did)
        self.assertEqual(row["outcome"], "CONFIRMED")
        self.assertEqual(row["window_start"], WINDOW)

    def test_venue_slot_exclusivity(self):
        self.confirmed()
        c = self.member(age=33, lo=25, hi=40, slots=0b1, gender="M", target="F")
        self.p.evaluate_pair(self.a, c, at=T0)
        pid2 = self.p.serve_pair(self.a, c, at=T0)
        self.p.decide(pid2, "A", "ACCEPT", at=T0)
        self.p.decide(pid2, "B", "ACCEPT", at=T0)
        did2 = self.p.open_date(pid2, at=T0)
        with self.assertRaises(BookingConflict):
            self.p.agree_venue(did2, 1, [self.venue], [self.venue], [self.venue],
                               window_start=WINDOW, at=T0)

    def test_no_common_venue_lapses(self):
        did, _ = self.confirmed()
        self.assertTrue(did)
        c = self.member(age=33, lo=25, hi=40, slots=0b1, gender="M", target="F")
        self.p.evaluate_pair(self.a, c, at=T0)
        pid2 = self.p.serve_pair(self.a, c, at=T0)
        self.p.decide(pid2, "A", "ACCEPT", at=T0)
        self.p.decide(pid2, "B", "ACCEPT", at=T0)
        did2 = self.p.open_date(pid2, at=T0)
        other = self.p.add_venue("SM", "Bar", "2 Main St")
        outcome = self.p.agree_venue(did2, 1, [self.venue, other],
                                     [other], [other], window_start=WINDOW, at=T0)
        self.assertEqual(outcome, "CONFIRMED")
        self.assertEqual(self.p._date(did2)["venue_id"], other)


class TestMeeting(Base):
    def test_checkin_window_enforced(self):
        did, _ = self.confirmed()
        with self.assertRaises(ProtocolError):
            self.p.check_in(did, "A", at="2026-11-05T17:00:00+00:00")
        self.p.check_in(did, "A", at="2026-11-05T18:50:00+00:00")
        self.assertEqual(self.p._date(did)["checkin_a"], 1)

    def test_finalize_statuses(self):
        did, _ = self.confirmed()
        self.p.check_in(did, "A", at="2026-11-05T18:55:00+00:00")
        self.assertEqual(self.p.finalize(did), "NO_SHOW_B")

    def test_reveal_requires_completion_and_both(self):
        did, _ = self.confirmed()
        self.p.check_in(did, "A", at="2026-11-05T18:55:00+00:00")
        self.p.check_in(did, "B", at="2026-11-05T18:56:00+00:00")
        self.assertEqual(self.p.finalize(did), "COMPLETED")

        # One-sided interest must not unlock anything.
        self.p.request_contact(did, "A")
        self.assertFalse(self.p.contact_exchange(did))

    def test_reveal_unlocks_when_both_ask(self):
        did, _ = self.confirmed()
        self.p.check_in(did, "A", at="2026-11-05T18:55:00+00:00")
        self.p.check_in(did, "B", at="2026-11-05T18:56:00+00:00")
        self.p.finalize(did)
        self.p.request_contact(did, "A")
        self.assertFalse(self.p.contact_exchange(did))
        self.p.request_contact(did, "B")
        self.assertTrue(self.p.contact_exchange(did))

    def test_contact_blocked_before_meeting(self):
        did, _ = self.confirmed()
        with self.assertRaises(ProtocolError):
            self.p.request_contact(did, "A")


class TestPurge(Base):
    def test_purge_removes_person_linked_rows(self):
        self.confirmed()
        self.p.purge(self.a)
        self.assertEqual(self.p.conn.execute(
            "SELECT COUNT(*) c FROM members").fetchone()["c"], 1)
        self.assertEqual(self.p.conn.execute(
            "SELECT COUNT(*) c FROM pairs").fetchone()["c"], 0)
        self.assertEqual(self.p.conn.execute(
            "SELECT COUNT(*) c FROM dates").fetchone()["c"], 0)
        self.assertEqual(self.p.conn.execute(
            "SELECT COUNT(*) c FROM retained_markers").fetchone()["c"], 1)


class TestPledges(Base):
    def test_pledge_requires_confirmed_date(self):
        pid = self.p.serve_pair(self.a, self.b, at=T0)
        self.p.decide(pid, "A", "ACCEPT", at=T0)
        self.p.decide(pid, "B", "ACCEPT", at=T0)
        did = self.p.open_date(pid, at=T0)
        with self.assertRaises(ProtocolError):
            self.p.pledge(did, "A", 2000, at=T0)

    def test_pledge_is_recorded_not_charged(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        self.p.pledge(did, "B", 1500, at=T0)
        summary = self.p.pledge_summary(did)
        self.assertEqual(summary["pledged_cents"], 3500)
        self.assertEqual(summary["sides"], {"A": "PLEDGED", "B": "PLEDGED"})
        # Nothing in the schema can hold funds; there is no balance column.
        cols = {r["name"] for r in self.p.conn.execute(
            "PRAGMA table_info(date_pledges)")}
        self.assertNotIn("provider", cols)
        self.assertNotIn("balance_cents", cols)

    def test_one_pledge_per_side(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        with self.assertRaises(ProtocolError):
            self.p.pledge(did, "A", 1000, at=T0)

    def test_amount_bounds(self):
        did, _ = self.confirmed()
        with self.assertRaises(ProtocolError):
            self.p.pledge(did, "A", 100, at=T0)
        with self.assertRaises(ProtocolError):
            self.p.pledge(did, "A", 50000, at=T0)

    def test_honored_pledge(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        self.p.mark_honored(did, "A", at="2026-11-05T19:10:00+00:00")
        self.assertEqual(self.p.pledge_summary(did)["honored_cents"], 2000)

    def test_broken_pledge_bans_immediately(self):
        did, _ = self.confirmed()
        expected = self.p._member_for_side(did, "A")
        self.p.pledge(did, "A", 2000, at=T0)
        # B is the decider for A's pledge.
        banned = self.p.report_broken(did, "B", at="2026-11-05T20:00:00+00:00")
        self.assertEqual(banned, expected)
        row = self.p.conn.execute(
            "SELECT status FROM members WHERE member_id=?", (expected,)).fetchone()
        self.assertEqual(row["status"], "banned")
        self.assertEqual(self.p.pledge_summary(did)["sides"]["A"], "BROKEN")

    def test_member_cannot_report_own_pledge(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        # A reporting means judging B's pledge, and B has none.
        with self.assertRaises(ProtocolError):
            self.p.report_broken(did, "A", at="2026-11-05T20:00:00+00:00")
        self.assertEqual(self.p.pledge_summary(did)["sides"]["A"], "PLEDGED")

    def test_audit_names_the_reporter(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        self.p.report_broken(did, "B", at="2026-11-05T20:00:00+00:00")
        row = self.p.conn.execute(
            "SELECT actor FROM audit WHERE action='ban_for_broken_pledge'").fetchone()
        self.assertEqual(row["actor"], self.p._member_for_side(did, "B"))

    def test_banned_member_cannot_be_matched_again(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        self.p.report_broken(did, "B", at="2026-11-05T20:00:00+00:00")
        with self.assertRaises(ProtocolError):
            self.p.evaluate_pair(self.a, self.b, at=T0)

    def test_cannot_report_before_window(self):
        did, _ = self.confirmed()
        self.p.pledge(did, "A", 2000, at=T0)
        with self.assertRaises(ProtocolError):
            self.p.report_broken(did, "B", at="2026-11-05T10:00:00+00:00")

    def _second_confirmed(self):
        c = self.member(age=35, lo=30, hi=45, slots=0b1, gender="M", target="F")
        self.p.evaluate_pair(self.a, c, at=T0)
        pid = self.p.serve_pair(self.a, c, at=T0)
        self.p.decide(pid, "A", "ACCEPT", at=T0)
        self.p.decide(pid, "B", "ACCEPT", at=T0)
        did = self.p.open_date(pid, at=T0)
        other = self.p.add_venue("SM", "Bar Two", "3 Main St")
        self.p.agree_venue(did, 1, [other], [other], [other],
                           window_start="2026-11-06T19:00:00+00:00", at=T0)
        return did


class TestMetrics(Base):
    def test_metrics_counts(self):
        self.confirmed()
        m = self.p.metrics()
        self.assertEqual(m["pairs"], 1)
        self.assertEqual(m["f"], 1.0)
        self.assertEqual(m["proposals_resolved"], 1)
        self.assertEqual(m["p"], 1.0)
        self.assertEqual(m["confirmed"], 1)


class TestPhoneBanKey(unittest.TestCase):
    """The ban outlives the profile because it is keyed to a hashed phone."""

    def setUp(self):
        self.p = Protocol.open()
        self.venue = self.p.add_venue("SM", "Cafe", "1 Main St")
        self.phone_a = "+13105550100"
        self.a = self._member("F", "M", self.phone_a)
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

    def _ban_a(self):
        side_of_a = "A" if self.p._member_for_side(self.did, "A") == self.a else "B"
        other = "B" if side_of_a == "A" else "A"
        self.p.pledge(self.did, side_of_a, 2000, at=T0)
        return self.p.report_broken(self.did, other,
                                    at="2026-11-05T20:00:00+00:00")

    def test_only_a_hash_is_stored(self):
        row = self.p.conn.execute(
            "SELECT phone_hash FROM members WHERE member_id=?", (self.a,)).fetchone()
        stored = row["phone_hash"]
        self.assertEqual(len(stored), 64)
        self.assertNotIn(self.phone_a, stored)
        int(stored, 16)  # valid hex

    def test_ban_blocks_same_phone_rejoin(self):
        self._ban_a()
        with self.assertRaises(ProtocolError):
            self._member("F", "M", self.phone_a)

    def test_another_phone_can_still_join(self):
        self._ban_a()
        self.assertTrue(self._member("F", "M", "+13105550199"))

    def test_purge_keeps_the_phone_key(self):
        marker = self.p.hash_phone(self.phone_a)
        self.p.purge(self.a)
        row = self.p.conn.execute(
            "SELECT kind FROM retained_markers WHERE marker_hash=?",
            (marker,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "verified")

    def test_salt_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/spx.db"
            p1 = Protocol.open(path)
            h1 = p1.hash_phone(self.phone_a)
            p1.close()
            p2 = Protocol.open(path)
            self.assertEqual(p2.hash_phone(self.phone_a), h1)
            p2.close()


if __name__ == "__main__":
    unittest.main()
