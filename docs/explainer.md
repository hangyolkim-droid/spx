# SPX, explained

SPX is a dating protocol built on one constraint: **two people meet before they
exchange a word.**

No chat. No feed. No swiping. No photos. A member sees a structured profile —
age, intent, availability, a district — and either accepts a proposed meeting or
does not. If both accept, they meet at a named public venue for forty-five
minutes. Contact details unlock only if both people ask afterwards.

That is the whole product. Everything below is the machinery that makes it
survive contact with reality.

---

## 1. Why remove chat

Dating apps are paid for attention, so their incentives point away from the
meeting. The better the match, the less you open the app. Chat is where that
tension lives: it is cheap to start, it costs nothing to abandon, and it creates
a category of relationship — the pen pal — that looks like progress and is not.

Removing chat is not a moral position. It is a filter. Text is where low-effort
actors fail: they cannot hold a normal conversation, they get bored, they say
something that ends it. Deleting that step does not delete bad actors; it
removes the step where they used to be caught cheaply.

SPX accepts that trade and changes the interface instead. There is no free-form
message to send, so there is nothing to ghost. The first costly, joint decision
is *where to meet* — and that decision is observable.

---

## 2. The protocol, as built

```
pair evaluated → proposal served → mutual accept → date opened
→ venue round → confirmed → check-in → completed → mutual reveal
```

Each transition has a rule and a test:

| Step | Rule | Why |
|---|---|---|
| Evaluate | five feasibility flags must all hold | no proposal is served to an impossible pair |
| Serve | one open proposal per pair | a pair is not re-spammed |
| Handshake | each side decides once; 12 hours | silence is a decision, not a limbo |
| Open date | only after mutual accept | no unilateral booking |
| Venue round | up to 3 rounds; both sides' accept sets intersect | negotiation, not assignment |
| Confirm | one confirmed date per venue per slot | the database refuses double-booking |
| Check-in | −15 / +45 minutes around the window | presence, not intention |
| Complete | both checked in, or a no-show is recorded | honest outcome |
| Reveal | both sides asked, after completion | contact is mutual or absent |

The 12-hour handshake matters more than it looks. A proposal that can sit
unanswered for a week is a proposal that generates anxiety and no data. Twelve
hours forces a decision, and a timeout is recorded as `TIMEOUT` rather than
being silently dropped.

The venue rule is also load-bearing: **there is no booking integration.** No
table is held, no API is called, no capacity is contracted. Two people are told
a named, public, staffed venue and a time. They find each other by the venue and
the hour. This removes the largest operational cost in the original design —
paying venues to hold prime-time tables — and keeps the safety property that
matters: a public place with staff present.

---

## 3. The five flags

A pair is feasible only when all five hold:

1. **Gender** — mutual target lists intersect.
2. **Age** — each person's age falls inside the other's stated range.
3. **Intent** — long-term / marriage / open, compatible; kids aligned (with
   `have_open` compatible with anything).
4. **Commute** — same district in v0. This is a simplification, not a claim
   about travel time.
5. **Availability** — the twelve half-hour slots overlap.

Feasibility is computed, stored, and never guessed at serve time. The metric
`f` — feasible pairs over evaluated pairs — is the single most useful number a
district can watch: it says whether there is anything to work with before
anyone complains about matches.

---

## 4. Money

SPX holds no money. There is no wallet, no card form, no payout, no provider
integration. A member **pledges** what they intend to spend at the venue — $5 to
$200 — and pays normally, on the night.

| | |
|---|---|
| What it is | a stated intention, recorded |
| Who charges | the venue, the usual way |
| What SPX stores | amount, side, honored or not |
| What SPX never stores | a card, a balance, a payout account, a provider reference |
| If honored | `mark_honored` records it |
| If broken | the member is banned immediately |

The consequence is membership, not debt. There is nobody to collect for and
nothing to collect. That is cheaper and more honest than escrow, and it avoids
turning a community protocol into a regulated payment business.

**Who decides a pledge was broken:** the other person on the date. A member can
never judge their own pledge — `report_broken` always evaluates the *opposite*
side's pledge, so self-reporting is structurally impossible.

---

## 5. Bans

Deleting an account is not a ban; a new profile takes thirty seconds. The
barrier is a hashed phone number:

- at signup, SPX stores `sha256(install_salt + phone)`, never the number;
- a broken pledge writes a `banned` marker keyed to that hash, valid five years;
- signup refuses a number with a live marker, before any row is created;
- purge keeps the phone-keyed marker, so a rejoin stays detectable.

The salt is generated once per database and never exported. Without it, a phone
hash is reversible by enumerating the number space; with it, two installs cannot
correlate the same person.

What this does not stop: a second SIM, a virtual number, a borrowed phone. At
one district and forty people, vouching does more work than the hash. The hash
earns its keep when a second district exists and nobody knows everybody.

Reinstating a banned member is an organizer action, requires a note, and is
written to the audit log. The code does not own that judgment; the host does.

---

## 6. Data

Purge is one transaction: profile, specs, availability, pairs, proposals, dates,
rounds, and pledges all go. What remains is a salted hash, a marker kind, and an
expiry — no link to who, where, or who they met.

The marker exists because the alternative is worse: a member with a prior safety
incident returning clean. It is disclosed on the graduation screen in the host's
own words, rather than advertised as a full deletion that did not happen.

---

## 7. What exists today

This repository is the protocol and the organizer console, not an app:

- `spx/domain.py` — feasibility, handshake, venue rounds, check-in, completion,
  mutual reveal, pledges, bans, purge.
- `schema/schema.sql` — tables, constraints, and the partial unique index that
  makes double-booking impossible.
- `spx/org.py` — localhost, token-gated console: dashboard, members, dates,
  audit, reinstate, cancel.
- 46 tests, standard library only, no runtime dependencies.

Not built: a mobile client, matching/ranking beyond pair feasibility, the
three-dates-per-month cap from the model, vouching, and identity verification.
Those are decisions, not omissions — each carries a cost explained in the README.

---

## 8. The hard parts

Code is the easy twenty percent.

1. **Liquidity.** A protocol with no members matches nobody. One district, one
   organizer, forty verified people, and a fixed weekly slot beats a global app
   with zero density. The metric to watch is not downloads; it is completed
   meetings per week.
2. **Safety operations.** A human who answers the phone, a venue list that gets
   re-walked, a documented escalation path. None of it is code, and a button
   that promises intervention without staffing it is worse than no button.
3. **Governance.** Who admits members, who reviews an incident, who can see the
   marker, and how that power is removed. Non-profit does not mean
   unaccountable.

If a fork ships this repository and no answer to those three, it has a schema,
not a service.

---

## 9. Honest open questions

- **Does the constraint work?** Nobody knows whether people will accept a blind
  meeting from a profile. That is the experiment, and it cannot be simulated.
- **Is the honor pledge enough?** It assumes most people keep their word when
  the alternative is losing the account. If it fails, the fix is not escrow;
  it is a smaller, more accountable district.
- **Does the ban key survive contact with real users?** A second SIM defeats it.
  Vouching is the stronger layer at pilot scale.
- **Is forty-five minutes right?** It is a guess, and a guess that is easy to
  change.
