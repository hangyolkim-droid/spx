"""District capacity simulator.

Answers one question before anyone recruits: **does this district have enough
feasible pairs to produce meetings?** Feasibility follows the same five flags as
`spx.domain`, but is computed in memory — a 400-person district is 80k pairs and
finishes instantly, where the domain would do 80k database writes.

    python3 -m tools.district_sim --members 120 --age-half 6
    python3 -m tools.district_sim --sweep
    python3 -m tools.district_sim --members 80 --proposals 8 --accept 0.5 --json

Everything is synthetic and stated as such. The output is a model, not evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass

SLOTS = 12


@dataclass(frozen=True)
class Person:
    gender: str
    target: str
    age: int
    lo: int
    hi: int
    intent: str
    kids: str
    slots: int


def generate(n: int, seed: int = 11, half: int = 6,
             age_min: int = 25, age_max: int = 45) -> list[Person]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        g = "M" if i % 2 == 0 else "F"
        age = rnd.randint(age_min, age_max)
        out.append(Person(
            gender=g, target="F" if g == "M" else "M", age=age,
            lo=age - half, hi=age + half,
            intent=rnd.choices(["long_term", "open", "marriage"],
                               [0.6, 0.25, 0.15])[0],
            kids=rnd.choices(["dont_want", "want", "have_open"],
                             [0.5, 0.3, 0.2])[0],
            slots=sum(1 << s for s in rnd.sample(range(SLOTS), 3)),
        ))
    return out


def _kids_ok(a: str, b: str) -> bool:
    return "have_open" in (a, b) or a == b


def flags(a: Person, b: Person) -> tuple[bool, bool, bool, bool, bool]:
    """The five flags, in the same order as the domain: gender, age, intent,
    commute, slots. Commute is same-district, i.e. always true in one district."""
    return (
        b.gender in a.target and a.gender in b.target,
        a.lo <= b.age <= a.hi and b.lo <= a.age <= b.hi,
        (a.intent == b.intent or "open" in (a.intent, b.intent))
        and _kids_ok(a.kids, b.kids),
        True,
        (a.slots & b.slots) != 0,
    )


def feasible(a: Person, b: Person) -> bool:
    return all(flags(a, b))


def _only_failing(a: Person, b: Person) -> int | None:
    """Index of the single failing flag, or None if zero or 2+ fail."""
    failed = [i for i, ok in enumerate(flags(a, b)) if not ok]
    return failed[0] if len(failed) == 1 else None


def simulate(persons: list[Person], proposals: int = 3,
             accept: float = 0.5) -> dict:
    n = len(persons)
    total = n * (n - 1) // 2
    feasible_pairs = 0
    single = [0, 0, 0, 0, 0]

    for i in range(n):
        a = persons[i]
        for j in range(i + 1, n):
            b = persons[j]
            f = flags(a, b)
            if all(f):
                feasible_pairs += 1
            else:
                bad = [k for k, ok in enumerate(f) if not ok]
                if len(bad) == 1:
                    single[bad[0]] += 1

    f_rate = feasible_pairs / total if total else 0.0
    partners = 2 * feasible_pairs / n if n else 0.0
    proposals_total = proposals * n / 2          # each pair consumes two slots
    meetings = proposals_total * accept * accept
    per_member = meetings / n if n else 0.0

    # per_member = (proposals / 2) * p^2, so 1 meeting needs 2 / p^2.
    needed = 2.0 / (accept * accept) if accept > 0 else math.inf

    return {
        "members": n,
        "pairs": total,
        "feasible_pairs": feasible_pairs,
        "f": round(f_rate, 4),
        "partners_per_member": round(partners, 2),
        "proposals_per_member": proposals,
        "accept": accept,
        "max_meetings": round(proposals_total, 1),
        "meetings": round(meetings, 1),
        "meetings_per_member": round(per_member, 3),
        "proposals_for_one_per_month": round(needed, 1),
        "single_flag": {
            "gender": single[0], "age": single[1], "intent": single[2],
            "commute": single[3], "slots": single[4],
        },
    }


def verdict(r: dict) -> str:
    p = r["partners_per_member"]
    if p < 3:
        return "dead pool: members exhaust their options in one cycle"
    if p < 8:
        return "thin: workable only with high acceptance rates"
    return "workable: enough options to survive a month"


def render(r: dict) -> str:
    lines = [
        f"district: {r['members']} members, {r['pairs']} pairs",
        f"feasible: {r['feasible_pairs']}  (f={r['f']})",
        f"partners per member: {r['partners_per_member']}",
        "",
        f"proposals/member/month: {r['proposals_per_member']}",
        f"maximum meetings/month: {r['max_meetings']} (even at 100% acceptance)",
        f"meetings/month at p={r['accept']}: {r['meetings']} "
        f"({r['meetings_per_member']}/member)",
        f"proposals needed for 1/member/month at p={r['accept']}: "
        f"{r['proposals_for_one_per_month']}",
        "  (per-member meetings depend on p, not on N: density raises p)",
        "",
        "pairs blocked by exactly one flag (leave-one-out uplift):",
    ]
    for k, v in sorted(r["single_flag"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k:<8} +{v}")
    lines += ["", "verdict: " + verdict(r)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="district_sim",
                                 description="SPX district capacity simulator")
    ap.add_argument("--members", type=int, default=120)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--age-half", type=int, default=6)
    ap.add_argument("--age-min", type=int, default=25)
    ap.add_argument("--age-max", type=int, default=45)
    ap.add_argument("--proposals", type=int, default=3,
                    help="proposals served per member per month")
    ap.add_argument("--accept", type=float, default=0.5,
                    help="probability a member accepts one proposal")
    ap.add_argument("--sweep", action="store_true", help="table across N")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.sweep:
        rows = []
        for n in (20, 40, 80, 120, 160, 240, 320, 400):
            ps = generate(n, args.seed, args.age_half, args.age_min, args.age_max)
            rows.append(simulate(ps, args.proposals, args.accept))
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print(f"{'N':>4} {'f':>6} {'partners':>9} {'meetings/mo':>12} "
              f"{'per member':>11}  verdict")
        for r in rows:
            print(f"{r['members']:>4} {r['f']:>6.3f} "
                  f"{r['partners_per_member']:>9.1f} {r['meetings']:>12.1f} "
                  f"{r['meetings_per_member']:>11.2f}  {verdict(r).split(':')[0]}")
        return 0

    persons = generate(args.members, args.seed, args.age_half,
                       args.age_min, args.age_max)
    r = simulate(persons, args.proposals, args.accept)
    print(json.dumps(r, indent=2) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
