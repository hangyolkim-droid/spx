# SPX — community edition

A meeting-first dating protocol. No chat, no feed, no swiping, no payments.

This repository is the **protocol and data model** for a non-profit, self-hosted
SPX instance. It is deliberately small: the rules live in one file, the schema in
another, and the whole thing runs on the standard library.

```
pair evaluated -> proposal served -> mutual accept -> date opened
-> venue round -> confirmed -> check-in -> completed -> mutual reveal
```

## What is enforced in code

| Rule | Where | Test |
|---|---|---|
| A pair is only servable when all five feasibility flags hold | `evaluate_pair`, `serve_pair` | `test_infeasible_pair_cannot_be_served` |
| A proposal expires after 12 h and resolves to `TIMEOUT` | `resolve` | `test_silence_expires_to_timeout` |
| One decision per side, no changes | `decide` | `test_double_decision_rejected` |
| A venue hosts at most one confirmed date per slot | partial unique index | `test_venue_slot_exclusivity` |
| Check-in only within −15/+45 min of the window | `check_in` | `test_checkin_window_enforced` |
| Contact unlocks only when completed **and** both asked | `contact_exchange` | `test_reveal_unlocks_when_both_ask` |
| Purge removes every person-linked row, keeps one salted marker | `purge` | `test_purge_removes_person_linked_rows` |
| A pledge is honor-based; a broken one bans the member immediately | `pledge`, `report_broken` | `test_broken_pledge_bans_immediately` |
| A ban is keyed to a hashed phone and outlives the profile | `hash_phone`, `is_banned_phone` | `test_ban_blocks_same_phone_rejoin` |

The venue-slot rule is a database constraint, not application logic, because
that is the one place where a race is both likely and socially expensive.

## What v0 deliberately does not do

- **No money handling of any kind.** No cards, no wallet, no provider, no
  settlement. A member pledges what they intend to spend at the venue, and the
  pledge is kept on their honor — see **Money** below.
- **No ID verification.** There is no vendor, no KYC, and no document storage.
  Accounts are community-vouched or district-organized. If a host wants
  verification, it belongs in a fork with a provider reference, not a column
  here.
- **No chat, by design.** The protocol's whole point. Any "just a quick
  message" feature deletes the product.
- **No booking integration.** Two people are told a named public venue and a
  time. There is no table to hold, no capacity contract, and no venue API.
- **No safety service.** SPX does not monitor dates, answer alarms, or
  intervene. If you are in danger, call 911. The only safety design here is
  structural: a public, staffed venue, a fixed window, and a check-in.

## Money

SPX touches no money. There is no card form, no wallet, no balance, no payout,
and no provider integration — not because they are hard to build, but because
holding funds would turn a community protocol into a regulated payment
business.

What exists is a **pledge**, in plain words:

| | |
|---|---|
| what it is | a stated intention to spend an amount at the venue on the date |
| how much | $5–$200 per side, one pledge per side |
| how it is paid | normally, by the member, at the venue, on the night |
| what SPX stores | the amount, the side, and whether it was honored |
| what SPX never stores | a card, a balance, a payout account, a provider reference |
| if it is honored | `mark_honored` records it |
| who decides | **the other person on the date.** A member can never judge their own pledge |
| if it is broken | the counterpart reports it; the member is **banned immediately** (`report_broken`) |

A broken pledge is not a debt. There is nothing to collect and no one to
collect it for. The consequence is membership: the account is banned on the
spot, and `evaluate_pair` refuses to serve a banned member again. That is the
whole enforcement mechanism, and it is cheaper and more honest than escrow.

The decider is the person who was on the other side of the table, not the one
who made the pledge. `report_broken(date, reporter_side)` always judges the
*opposite* side's pledge, so self-reporting is structurally impossible: if you
file, and the other side has no open pledge, the call fails and changes
nothing.

A report cannot be filed before the meeting window opens, so a matched pair
cannot pre-emptively ban each other. There is no automatic appeal: reinstating
a member is an organizer action, written to `audit` with the reporter's id —
which means the host's judgment, not the code, owns that decision.

## Bans

Deleting an account is not a ban, and banning an account is not a barrier if a
new profile takes thirty seconds. The barrier is a **hashed phone number**:

| | |
|---|---|
| stored at signup | `sha256(install_salt + normalized_phone)` — 64 hex chars, no number |
| salt | generated once per database, stored in `settings`, never exported |
| on a broken pledge | a `banned` marker keyed to that hash, valid **5 years** |
| on signup | `is_banned_phone` refuses the same number before a row is created |
| on purge | the marker is keyed to the phone hash, so rejoining stays detectable |
| different number | allowed — this stops the lazy rejoin, not a determined one |

The salt matters: without it, a hash of a phone number is trivially reversible
by enumerating the number space. With a per-install salt, two SPX databases
cannot correlate the same person, and a leaked table cannot be brute-forced
without the salt.

What this does **not** stop: a second SIM, a virtual number, or a borrowed
phone. At one district and forty people, the voucher requirement does more work
than the hash — the organizer knows who invited whom. The hash matters when a
second district exists and nobody knows everybody.

## Run it

No dependencies, Python 3.10+.

```sh
python3 -m spx.demo          # six members, one full meeting
python3 -m unittest discover -s tests -v
```

Library use:

```python
from spx.domain import Protocol

p = Protocol.open("spx.db")          # or ":memory:"
a = p.add_member(district="SM", gender="F", target_gender="M", age=31,
                 target_age_min=25, target_age_max=40, intent="long_term",
                 kids="dont_want", slot_bitmask=0b1)
b = p.add_member(district="SM", gender="M", target_gender="F", age=34,
                 target_age_min=25, target_age_max=40, intent="long_term",
                 kids="dont_want", slot_bitmask=0b1)
pid = p.serve_pair(a, b)
```

## Layout

```
schema/schema.sql   tables, constraints, the venue-slot unique index
spx/domain.py       feasibility, handshake, venue rounds, check-in, reveal, purge
spx/org.py          organizer console (stdlib HTTP, localhost, token-gated)
spx/demo.py         end-to-end walkthrough
tests/              20 invariant tests, stdlib unittest
```

## Organizer console

The protocol has no admin UI by design — but a district needs a human who can
see the pool and reverse a bad call. That is this:

```sh
SPX_ORG_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  python3 -m spx.org --db spx.db --port 8787
```

Open the printed URL once; the token is stored in an `HttpOnly`, `SameSite=Strict`
cookie. Binds `127.0.0.1` unless `--host` is passed explicitly.

| page | what it shows |
|---|---|
| `/` | pool by status, open proposals, confirmed dates, active bans, f / p rates |
| `/members` | every member, with a **reinstate** action for banned ones |
| `/dates` | confirmed and past dates, with a **cancel** action |
| `/audit` | last 100 actions, including the organizer's note |

Two rules the console enforces on itself:

- **Reinstate requires a note.** It also deletes the phone-keyed ban marker, or
  the member would stay blocked at signup despite an active status.
- **Cancel is not a punishment.** A cancelled date voids open pledges
  (`VOID`), never marks them `BROKEN` — nobody is banned because a venue
  flooded.

Every action writes an `audit` row naming the organizer action and the note.
There is no free-text profile data anywhere, so nothing private can leak
through a page. The console uses a short-lived SQLite connection per request,
because `sqlite3` objects cannot cross threads and the server is threaded.

## Data handling

Purge is a single transaction: profile, specs, availability, pairs, proposals,
dates, and round rows all go. What remains is a salted hash, a marker kind, and
an expiry — no link to who, where, or who they met. The marker exists so a
prior ban is not invisible on rejoin; it is disclosed on the
graduation screen in the host's own words.

## The hard part is not in this repo

Code is the easy 20% of this product. The other 80% is:

1. **Liquidity.** A protocol with no members matches nobody. One district, one
   organizer, forty verified people, and a fixed weekly slot beats a global app
   with zero density.
2. **Trust operations.** A venue list that is walked and re-checked, an
   organizer who knows the district, and a written answer for what happens when
   someone is banned by mistake. None of it is code.
3. **Governance.** Who admits members, who decides an appeal, who can see the
   marker, and how that power is removed. Non-profit does not mean unaccountable.

If a fork ships only this repository and no answer to those three, it is a
schema, not a service.

## License

**AGPL-3.0.** For a network service this is the license that does what a
non-profit edition needs: anyone who hosts a modified SPX must publish their
changes. A permissive license would let a host take the protocol closed and
keep the protocol fixes to themselves.
