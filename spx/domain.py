"""SPX protocol core — the rules, independent of any web framework.

Everything here is deterministic and testable. The state machine is:

    pair evaluated -> proposal served -> mutual accept -> date opened
    -> venue round -> confirmed -> check-in -> completed -> mutual reveal

Design rules that are enforced in code, not documentation:

* A pair is only servable when all five feasibility flags hold.
* A proposal expires after 12 hours and becomes TIMEOUT (R9).
* A venue slot can hold at most one confirmed date. The DB enforces it.
* Contact details unlock only when the meeting completed *and* both sides asked.
* Purging a member removes every person-linked row, keeping one salted marker.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "schema.sql"

HANDSHAKE_HOURS = 12
CHECKIN_EARLY_MIN = 15
CHECKIN_LATE_MIN = 45
MAX_ROUNDS = 3
SALT_KEY = "hash_salt"
BAN_YEARS = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ProtocolError(Exception):
    """A rule was violated. Always a caller bug, never a data condition."""


class BookingConflict(ProtocolError):
    """The venue already hosts a confirmed date in that slot."""


class Protocol:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- construction -----------------------------------------------------
    @classmethod
    def open(cls, path: str | Path = ":memory:",
             salt: str | None = None) -> "Protocol":
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_PATH.read_text())
        proto = cls(conn)
        proto._ensure_salt(salt)
        return proto

    def _ensure_salt(self, salt: str | None = None) -> None:
        """Install-local hash salt. Generated once; never leaves this database."""
        if salt:
            self._salt = salt
            return
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (SALT_KEY,)).fetchone()
        if row:
            self._salt = row["value"]
            return
        self._salt = secrets.token_hex(32)
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?)", (SALT_KEY, self._salt))
        self.conn.commit()

    def hash_phone(self, phone: str) -> str:
        """Stable ban key. The number itself is never stored anywhere."""
        normalized = "".join(c for c in phone if c.isdigit() or c == "+")
        if len(normalized) < 7:
            raise ProtocolError("not a usable phone number")
        return hashlib.sha256(f"{self._salt}:{normalized}".encode()).hexdigest()

    def is_banned_phone(self, phone: str, at: str | None = None) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM retained_markers
               WHERE marker_hash=? AND kind='banned' AND expires_at > ?""",
            (self.hash_phone(phone), at or utcnow()),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.conn.close()

    # -- members ----------------------------------------------------------
    def add_member(
        self,
        *,
        district: str,
        gender: str,
        target_gender: str,
        age: int,
        target_age_min: int,
        target_age_max: int,
        intent: str,
        kids: str,
        slot_bitmask: int,
        member_id: str | None = None,
        phone: str | None = None,
        at: str | None = None,
    ) -> str:
        if age < 18:
            raise ProtocolError("members must be adults")
        phone_hash = None
        if phone:
            phone_hash = self.hash_phone(phone)
            if self.is_banned_phone(phone, at):
                raise ProtocolError("this number is barred from the platform")
        mid = member_id or uuid.uuid4().hex
        try:
            self.conn.execute(
                """INSERT INTO members (member_id, district, gender, target_gender,
                       age, target_age_min, target_age_max, intent, kids,
                       slot_bitmask, phone_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, district, gender, target_gender, age, target_age_min,
                 target_age_max, intent, kids, slot_bitmask, phone_hash,
                 at or utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ProtocolError("that number is already registered") from exc
        self.conn.commit()
        return mid

    def add_venue(self, district: str, name: str, address: str,
                  priority: int = 100, venue_id: str | None = None) -> str:
        vid = venue_id or uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO venues (venue_id, district, name, address, priority) "
            "VALUES (?,?,?,?,?)",
            (vid, district, name, address, priority),
        )
        self.conn.commit()
        return vid

    # -- feasibility ------------------------------------------------------
    def evaluate_pair(self, a: str, b: str, at: str | None = None) -> dict:
        ma, mb = self._member(a), self._member(b)
        if ma["status"] != "active" or mb["status"] != "active":
            raise ProtocolError("both members must be active")
        lo, hi = sorted([a, b])

        def wants(me, other) -> bool:
            targets = {t.strip() for t in me["target_gender"].split(",")}
            return other["gender"] in targets

        gender_ok = int(wants(ma, mb) and wants(mb, ma))
        age_ok = int(
            ma["target_age_min"] <= mb["age"] <= ma["target_age_max"]
            and mb["target_age_min"] <= ma["age"] <= mb["target_age_max"]
        )
        intent_ok = int(
            (ma["intent"] == mb["intent"] or "open" in (ma["intent"], mb["intent"]))
            and self._kids_ok(ma["kids"], mb["kids"])
        )
        commute_ok = int(ma["district"] == mb["district"])
        slot_ok = int((ma["slot_bitmask"] & mb["slot_bitmask"]) != 0)

        flags = dict(gender_ok=gender_ok, age_ok=age_ok, intent_ok=intent_ok,
                     commute_ok=commute_ok, slot_ok=slot_ok)
        feasible = int(all(flags.values()))
        pair_id = hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()[:24]
        self.conn.execute(
            """INSERT INTO pairs (pair_id, a, b, gender_ok, age_ok, intent_ok,
                   commute_ok, slot_ok, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(a, b) DO UPDATE SET
                   gender_ok=excluded.gender_ok, age_ok=excluded.age_ok,
                   intent_ok=excluded.intent_ok, commute_ok=excluded.commute_ok,
                   slot_ok=excluded.slot_ok, evaluated_at=excluded.evaluated_at""",
            (pair_id, lo, hi, gender_ok, age_ok, intent_ok, commute_ok,
             slot_ok, at or utcnow()),
        )
        self.conn.commit()
        return {"pair_id": pair_id, **flags, "feasible": feasible}

    @staticmethod
    def _kids_ok(a: str, b: str) -> bool:
        # "have_open" is compatible with anything; want/dont_want must agree.
        if "have_open" in (a, b):
            return True
        return a == b

    # -- proposals --------------------------------------------------------
    def serve_pair(self, a: str, b: str, at: str | None = None) -> str:
        pair = self.evaluate_pair(a, b, at)
        if not pair["feasible"]:
            raise ProtocolError("pair is not feasible")
        open_row = self.conn.execute(
            """SELECT p.proposal_id FROM proposals p
               WHERE p.pair_id = ?
                 AND (p.decision_a IS NULL OR p.decision_b IS NULL)
                 AND p.deadline_at > ?""",
            (pair["pair_id"], at or utcnow()),
        ).fetchone()
        if open_row:
            raise ProtocolError("an open proposal already exists for this pair")

        pid = uuid.uuid4().hex
        start = parse(at or utcnow())
        self.conn.execute(
            "INSERT INTO proposals (proposal_id, pair_id, served_at, deadline_at) "
            "VALUES (?,?,?,?)",
            (pid, pair["pair_id"], start.isoformat(),
             (start + timedelta(hours=HANDSHAKE_HOURS)).isoformat()),
        )
        self.conn.commit()
        return pid

    def decide(self, proposal_id: str, side: str, decision: str,
               at: str | None = None) -> None:
        if decision not in ("ACCEPT", "DECLINE", "TIMEOUT"):
            raise ProtocolError("bad decision")
        side = side.upper()
        if side not in ("A", "B"):
            raise ProtocolError("side must be A or B")
        prop = self._proposal(proposal_id)
        ts = at or utcnow()
        if parse(ts) > parse(prop["deadline_at"]):
            raise ProtocolError("proposal deadline passed")

        col, tcol = f"decision_{side.lower()}", f"decided_at_{side.lower()}"
        cur = self.conn.execute(
            f"UPDATE proposals SET {col}=?, {tcol}=? "
            f"WHERE proposal_id=? AND {col} IS NULL",
            (decision, ts, proposal_id),
        )
        if cur.rowcount != 1:
            raise ProtocolError("decision already recorded for that side")
        self.conn.commit()

    def resolve(self, proposal_id: str, at: str | None = None) -> str:
        prop = self._proposal(proposal_id)
        ts = at or utcnow()
        expired = parse(ts) > parse(prop["deadline_at"])

        da, db = prop["decision_a"], prop["decision_b"]
        if expired:
            if da is None:
                self.conn.execute(
                    "UPDATE proposals SET decision_a='TIMEOUT', decided_at_a=? "
                    "WHERE proposal_id=? AND decision_a IS NULL", (ts, proposal_id))
                da = "TIMEOUT"
            if db is None:
                self.conn.execute(
                    "UPDATE proposals SET decision_b='TIMEOUT', decided_at_b=? "
                    "WHERE proposal_id=? AND decision_b IS NULL", (ts, proposal_id))
                db = "TIMEOUT"
            self.conn.commit()

        if "DECLINE" in (da, db):
            return "DECLINED"
        if da == "TIMEOUT" or db == "TIMEOUT":
            return "TIMEOUT"
        if da == "ACCEPT" and db == "ACCEPT":
            return "MUTUAL_ACCEPT"
        return "PENDING"

    # -- dates ------------------------------------------------------------
    def open_date(self, proposal_id: str, at: str | None = None) -> str:
        if self.resolve(proposal_id, at) != "MUTUAL_ACCEPT":
            raise ProtocolError("proposal is not a mutual accept")
        existing = self.conn.execute(
            "SELECT date_id FROM dates WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if existing:
            return existing["date_id"]
        did = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO dates (date_id, proposal_id) VALUES (?,?)",
            (did, proposal_id),
        )
        self.conn.commit()
        return did

    def agree_venue(self, date_id: str, round_no: int, offered: list[str],
                    accept_a: list[str], accept_b: list[str],
                    window_start: str, at: str | None = None) -> str:
        """Run one venue round. Confirms the date or lapses it.

        `window_start` is the mutual availability slot, already reduced to one
        concrete half-hour. The DB enforces venue-slot exclusivity.
        """
        if not 1 <= round_no <= MAX_ROUNDS:
            raise ProtocolError("round must be 1..3")
        if round_no > 1:
            prev = self.conn.execute(
                "SELECT 1 FROM venue_rounds WHERE date_id=? AND round_no=?",
                (date_id, round_no - 1)).fetchone()
            if not prev:
                raise ProtocolError("rounds must be sequential")

        common = sorted(set(offered) & set(accept_a) & set(accept_b))
        outcome = "CONFIRMED" if common else "NO_COMMON_VENUE"
        agreed = common[0] if common else None

        rid = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO venue_rounds (round_id, date_id, round_no, offered,
                   accept_a, accept_b, intersection, outcome, agreed_venue_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, date_id, round_no, json.dumps(offered), json.dumps(accept_a),
             json.dumps(accept_b), len(common), outcome, agreed),
        )

        if not common:
            self.conn.execute(
                "UPDATE dates SET outcome='LAPSED', lapse_cause='NO_COMMON_VENUE' "
                "WHERE date_id=?", (date_id,))
            self.conn.commit()
            return outcome

        try:
            self.conn.execute(
                """UPDATE dates SET venue_id=?, window_start=?, outcome='CONFIRMED'
                   WHERE date_id=?""",
                (agreed, window_start, date_id))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise BookingConflict(
                "that venue already has a confirmed date in this slot") from exc
        return outcome

    def check_in(self, date_id: str, side: str, at: str | None = None) -> None:
        row = self._date(date_id)
        if row["outcome"] != "CONFIRMED":
            raise ProtocolError("date is not confirmed")
        ts = parse(at or utcnow())
        start = parse(row["window_start"])
        if not (start - timedelta(minutes=CHECKIN_EARLY_MIN)
                <= ts <= start + timedelta(minutes=CHECKIN_LATE_MIN)):
            raise ProtocolError("outside the check-in window")
        col = "checkin_a" if side.upper() == "A" else "checkin_b"
        self.conn.execute(
            f"UPDATE dates SET {col}=1 WHERE date_id=? AND {col}=0", (date_id,))
        self.conn.commit()

    def finalize(self, date_id: str) -> str:
        row = self._date(date_id)
        if row["outcome"] != "CONFIRMED":
            raise ProtocolError("date is not confirmed")
        a, b = row["checkin_a"], row["checkin_b"]
        status = ("COMPLETED" if a and b else
                  "NO_SHOW_B" if a else
                  "NO_SHOW_A" if b else "MUTUAL_NO_SHOW")
        self.conn.execute(
            "UPDATE dates SET meeting_status=? WHERE date_id=?", (status, date_id))
        self.conn.commit()
        return status

    def request_contact(self, date_id: str, side: str) -> None:
        row = self._date(date_id)
        if row["meeting_status"] != "COMPLETED":
            raise ProtocolError("contact exchange requires a completed meeting")
        col = "req_a" if side.upper() == "A" else "req_b"
        self.conn.execute(
            f"UPDATE dates SET {col}=1 WHERE date_id=?", (date_id,))
        self.conn.commit()

    def contact_exchange(self, date_id: str) -> bool:
        row = self._date(date_id)
        return bool(row["meeting_status"] == "COMPLETED"
                    and row["req_a"] and row["req_b"])

    # -- pledges (honor system) -------------------------------------------
    def pledge(self, date_id: str, side: str, amount_cents: int,
               at: str | None = None) -> str:
        """Record an honor pledge: what this side says they will spend.

        No money changes hands here. SPX holds nothing, charges nothing, and
        never sees a card. The amount is a commitment the member keeps — or
        does not, which costs them the account.
        """
        row = self._date(date_id)
        if row["outcome"] != "CONFIRMED":
            raise ProtocolError("a pledge requires a confirmed date")
        if row["meeting_status"] is not None:
            raise ProtocolError("the meeting is already closed")
        if not 500 <= amount_cents <= 20000:
            raise ProtocolError("pledge must be between $5 and $200")

        pid = uuid.uuid4().hex
        try:
            self.conn.execute(
                """INSERT INTO date_pledges (pledge_id, date_id, side,
                       amount_cents, created_at) VALUES (?,?,?,?,?)""",
                (pid, date_id, side.upper(), amount_cents, at or utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ProtocolError("that side has already pledged for this date") from exc
        self.conn.commit()
        return pid

    def mark_honored(self, date_id: str, side: str,
                     at: str | None = None) -> None:
        cur = self.conn.execute(
            """UPDATE date_pledges SET status='HONORED', resolved_at=?
               WHERE date_id=? AND side=? AND status='PLEDGED'""",
            (at or utcnow(), date_id, side.upper()),
        )
        if cur.rowcount != 1:
            raise ProtocolError("no open pledge for that side")
        self.conn.commit()

    def report_broken(self, date_id: str, reporter_side: str,
                      at: str | None = None) -> str:
        """The counterpart decides whether the other side's pledge was kept.

        `reporter_side` is the person filing the report; the pledge under
        review belongs to the *other* side. A member can never report their own
        pledge, and the report cannot be filed before the meeting window opens.

        Returns the banned member_id. There is no automatic appeal: reinstating
        a member is an organizer action, recorded in `audit`.
        """
        row = self._date(date_id)
        if row["outcome"] != "CONFIRMED":
            raise ProtocolError("no confirmed date to report on")
        ts = at or utcnow()
        if parse(ts) < parse(row["window_start"]):
            raise ProtocolError("cannot report before the meeting window")

        reporter = reporter_side.upper()
        if reporter not in ("A", "B"):
            raise ProtocolError("side must be A or B")
        # The decider is the person on the other side of the table.
        target = "B" if reporter == "A" else "A"

        cur = self.conn.execute(
            """UPDATE date_pledges SET status='BROKEN', resolved_at=?
               WHERE date_id=? AND side=? AND status='PLEDGED'""",
            (ts, date_id, target),
        )
        if cur.rowcount != 1:
            raise ProtocolError("the other side has no open pledge to report")

        member_id = self._member_for_side(date_id, target)
        self.conn.execute(
            "UPDATE members SET status='banned' WHERE member_id=?", (member_id,))
        # The ban outlives the profile: key it to the phone hash when there is
        # one, so a fresh profile from the same number is refused at signup.
        row = self.conn.execute(
            "SELECT phone_hash FROM members WHERE member_id=?", (member_id,)
        ).fetchone()
        marker = row["phone_hash"] or hashlib.sha256(
            f"spx-marker:{member_id}".encode()).hexdigest()
        expires = (parse(ts) + timedelta(days=365 * BAN_YEARS)).isoformat()
        self.conn.execute(
            """INSERT OR REPLACE INTO retained_markers
                   (marker_hash, kind, created_at, expires_at)
               VALUES (?, 'banned', ?, ?)""",
            (marker, ts, expires),
        )
        self.conn.execute(
            "INSERT INTO audit (at, actor, action, subject) VALUES (?,?,?,?)",
            (ts, self._member_for_side(date_id, reporter),
             "ban_for_broken_pledge", member_id))
        self.conn.commit()
        return member_id

    def pledge_summary(self, date_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT side, amount_cents, status FROM date_pledges WHERE date_id=?",
            (date_id,)).fetchall()
        return {
            "pledged_cents": sum(r["amount_cents"] for r in rows
                                 if r["status"] == "PLEDGED"),
            "honored_cents": sum(r["amount_cents"] for r in rows
                                 if r["status"] == "HONORED"),
            "sides": {r["side"]: r["status"] for r in rows},
        }

    # -- purge ------------------------------------------------------------
    def purge(self, member_id: str) -> None:
        """Remove every person-linked row; keep one de-identified marker (R1)."""
        member = self._member(member_id)
        self.conn.execute("DELETE FROM members WHERE member_id=?", (member_id,))
        # Prefer the phone hash: it is what makes a rejoin detectable.
        marker = (member["phone_hash"]
                  or hashlib.sha256(f"spx-marker:{member_id}".encode()).hexdigest())
        exp = (datetime.now(timezone.utc) + timedelta(days=730)).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO retained_markers "
            "(marker_hash, kind, created_at, expires_at) VALUES (?,?,?,?)",
            (marker, "verified", utcnow(), exp))
        self.conn.execute(
            "INSERT INTO audit (at, actor, action, subject) VALUES (?,?,?,?)",
            (utcnow(), "system", "purge", None))
        self.conn.commit()

    # -- metrics ----------------------------------------------------------
    def metrics(self) -> dict:
        q = self.conn.execute
        pairs = q("SELECT COUNT(*) n, COALESCE(SUM(feasible),0) f FROM pairs").fetchone()
        props = q("""SELECT COUNT(*) n,
                            COALESCE(SUM(decision_a='ACCEPT' AND decision_b='ACCEPT'),0) m
                     FROM proposals
                     WHERE decision_a IS NOT NULL AND decision_b IS NOT NULL""").fetchone()
        dates = q("""SELECT COUNT(*) n,
                            COALESCE(SUM(outcome='CONFIRMED'),0) c,
                            COALESCE(SUM(meeting_status='COMPLETED'),0) done,
                            COALESCE(SUM(meeting_status='COMPLETED' AND req_a=1 AND req_b=1),0) reveal
                     FROM dates""").fetchone()
        return {
            "pairs": pairs["n"],
            "feasible": pairs["f"],
            "f": round(pairs["f"] / pairs["n"], 4) if pairs["n"] else None,
            "proposals_resolved": props["n"],
            "mutual_accepts": props["m"],
            "p": round(props["m"] / props["n"], 4) if props["n"] else None,
            "dates": dates["n"],
            "confirmed": dates["c"],
            "completed": dates["done"],
            "mutual_reveals": dates["reveal"],
        }

    # -- helpers ----------------------------------------------------------
    def _member(self, member_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not row:
            raise ProtocolError("unknown member")
        return row

    def _proposal(self, proposal_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            raise ProtocolError("unknown proposal")
        return row

    def _date(self, date_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM dates WHERE date_id=?", (date_id,)).fetchone()
        if not row:
            raise ProtocolError("unknown date")
        return row

    def _member_for_side(self, date_id: str, side: str) -> str:
        """Side A is the pair's canonical first member, side B the second."""
        row = self.conn.execute(
            """SELECT pr.a, pr.b
                 FROM dates d
                 JOIN proposals p ON p.proposal_id = d.proposal_id
                 JOIN pairs pr ON pr.pair_id = p.pair_id
                WHERE d.date_id = ?""",
            (date_id,),
        ).fetchone()
        if not row:
            raise ProtocolError("unknown date")
        return row["a"] if side.upper() == "A" else row["b"]
