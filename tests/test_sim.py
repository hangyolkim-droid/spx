"""The simulator must agree with the domain's own feasibility rules."""

import unittest

from spx.domain import Protocol
from tools.district_sim import feasible, generate, simulate


class TestSimMatchesDomain(unittest.TestCase):
    def test_feasible_count_matches_domain(self):
        people = generate(30, seed=3)
        p = Protocol.open()
        ids = []
        for person in people:
            ids.append(p.add_member(
                district="SM", gender=person.gender,
                target_gender=person.target, age=person.age,
                target_age_min=person.lo, target_age_max=person.hi,
                intent=person.intent, kids=person.kids,
                slot_bitmask=person.slots))

        domain = sum(
            1 for i in range(30) for j in range(i + 1, 30)
            if p.evaluate_pair(ids[i], ids[j])["feasible"])
        sim = sum(
            1 for i in range(30) for j in range(i + 1, 30)
            if feasible(people[i], people[j]))
        self.assertEqual(domain, sim)


class TestMeetingMath(unittest.TestCase):
    def test_eight_proposals_at_half_acceptance_is_one_per_member(self):
        r = simulate(generate(50, seed=1), proposals=8, accept=0.5)
        # per_member = (proposals / 2) * p^2 = 4 * 0.25 = 1.0
        self.assertAlmostEqual(r["meetings_per_member"], 1.0, places=3)
        self.assertAlmostEqual(r["proposals_for_one_per_month"], 8.0)

    def test_three_proposals_cannot_exceed_one_and_a_half(self):
        r = simulate(generate(80, seed=1), proposals=3, accept=1.0)
        self.assertAlmostEqual(r["meetings_per_member"], 1.5, places=3)

    def test_density_raises_options_not_per_member_meetings(self):
        small = simulate(generate(40, seed=5))
        large = simulate(generate(160, seed=5))
        self.assertGreater(large["partners_per_member"],
                           small["partners_per_member"] * 3)
        self.assertAlmostEqual(small["meetings_per_member"],
                               large["meetings_per_member"], places=3)


if __name__ == "__main__":
    unittest.main()
