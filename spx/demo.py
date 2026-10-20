"""End-to-end demo: six members, one meeting, no network.

    python -m spx.demo
"""

from spx.domain import Protocol

SLOTS = 0b000000000001  # Thu 18:30


def main() -> None:
    p = Protocol.open()
    venues = [
        p.add_venue("SM", "Cafe Luna", "1400 Main St", priority=10),
        p.add_venue("SM", "The Library Bar", "22 Ocean Ave", priority=20),
        p.add_venue("SM", "Verde", "8 Sunset Blvd", priority=30),
    ]

    def member(**over):
        base = dict(district="SM", gender="F", target_gender="M", age=31,
                    target_age_min=25, target_age_max=40, intent="long_term",
                    kids="dont_want", slot_bitmask=SLOTS)
        base.update(over)
        return p.add_member(**base)

    a = member(age=31, gender="F", target_gender="M")
    b = member(age=34, gender="M", target_gender="F")
    member(age=29, gender="M", target_gender="F", target_age_min=30)  # age-gated
    member(age=41, gender="M", target_gender="F")
    member(age=33, gender="M", target_gender="F", kids="want")       # kids conflict

    print("members:", p.conn.execute("SELECT COUNT(*) c FROM members").fetchone()["c"])

    pair = p.evaluate_pair(a, b, at="2026-11-01T12:00:00+00:00")
    print("pair feasible:", pair["feasible"], {k: v for k, v in pair.items()
                                               if k.endswith("_ok")})

    pid = p.serve_pair(a, b, at="2026-11-01T12:00:00+00:00")
    print("proposal served:", pid[:8], "expires in 12h")

    p.decide(pid, "A", "ACCEPT", at="2026-11-01T12:30:00+00:00")
    p.decide(pid, "B", "ACCEPT", at="2026-11-01T13:00:00+00:00")
    print("handshake:", p.resolve(pid, at="2026-11-01T13:00:00+00:00"))

    did = p.open_date(pid, at="2026-11-01T13:00:00+00:00")
    outcome = p.agree_venue(
        did, 1,
        offered=venues,
        accept_a=[venues[0], venues[1]],
        accept_b=[venues[1], venues[2]],
        window_start="2026-11-05T19:00:00+00:00",
        at="2026-11-01T13:05:00+00:00",
    )
    row = p._date(did)
    venue = p.conn.execute("SELECT name FROM venues WHERE venue_id=?",
                           (row["venue_id"],)).fetchone()["name"]
    print(f"venue round 1: {outcome} -> {venue} at {row['window_start']}")

    p.pledge(did, "A", 2000, at="2026-11-01T13:10:00+00:00")
    p.pledge(did, "B", 1500, at="2026-11-01T13:12:00+00:00")
    print("pledged toward the date:", p.pledge_summary(did)["pledged_cents"], "cents")

    p.check_in(did, "A", at="2026-11-05T18:55:00+00:00")
    p.check_in(did, "B", at="2026-11-05T18:57:00+00:00")
    p.mark_honored(did, "A", at="2026-11-05T19:45:00+00:00")
    p.mark_honored(did, "B", at="2026-11-05T19:45:00+00:00")
    print("meeting:", p.finalize(did))

    p.request_contact(did, "A")
    print("one-sided reveal:", p.contact_exchange(did))
    p.request_contact(did, "B")
    print("mutual reveal:", p.contact_exchange(did))

    print("metrics:", p.metrics())


if __name__ == "__main__":
    main()
