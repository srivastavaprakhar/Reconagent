# Pitch video outline — target under 5:00

Structure only — not a script. Each beat has a target duration, the point it
has to land, and whether it needs an on-screen visual or works as narration
over the terminal/code. Numbers below are pulled directly from
`reports/eval_report.md` and `ARCHITECTURE.md` — verify against those files if
either is regenerated before recording, don't requote from memory.

---

## 0:00–0:30 — Open on the regulatory stakes, not the product

**Say:** "A bank doesn't credit a merchant per payment — it sweeps several
settlements under one net transfer. Cross a border and it gets worse: the
remitter's name arrives mangled through SWIFT, the FX rate applied is a fact
you're handed, not one you can trust, and every export receipt carries a
silent regulatory clock — RBI requires it be matched to its shipping bill and
marked realised, or the exporter gets caution-listed. That's not a
bookkeeping nuisance. That's a compliance failure with a real deadline."

**Why open here, not on the product:** every reconciliation entry in this
track can open on "we match settlements to bank statements." Almost none can
open on EDPMS, because almost none built it. Leading with the regulatory
stake immediately signals this isn't the generic pitch.

**Visual:** none needed — a strong cold open works better as direct address
than as a slide. If anything, a single still frame: the EDPMS shipping-bill
deadline concept, one sentence, no chart yet.

---

## 0:30–1:00 — The differentiation claim, stated plainly

**Say:** "Deterministic-first matching, an LLM confined to explanation only,
an honest exception list, false-match rate as the headline metric — that's
table stakes in this track now. What isn't table stakes: an FX-tolerance
validator whose band is *derived* from labelled data instead of hand-set, a
variance decomposition that attributes a gap mathematically instead of
guessing, and the EDPMS linkage nobody else in this space has built. We
didn't build a reconciliation engine — we built the one that handles what
happens the moment a payment crosses a border."

**Visual:** a single comparison line/table — "generic domestic recon" vs.
"this system" — three or four rows max (FX validation, variance attribution,
EDPMS, false-match headline). Cheap to build, does real work in 15 seconds.

---

## 1:00–2:00 — Walk one real subset-sum bundle

**Say, over the terminal or a rendered diagram of case `MAIN-00003`:** "A bank
doesn't tell you which settlements a credit covers — sometimes you have to
work it out. Here, one credit sweeps two settlements. There's also a decoy
pair in the same pool that sums to within 3 minor units of the same credit —
close enough that a solver taking the first match inside tolerance would pick
the wrong pair. Ours doesn't. It enumerates every admissible subset and takes
the one with zero residual — and we didn't just build that, we proved it: a
deliberately naive first-fit solver, run against the same data, gets 8 of our
12 main-set bundles wrong. Ours gets zero wrong, on either the main set or a
harder adversarial holdout we never tuned against."

**Concrete numbers to have on screen:** the two real settlement amounts, the
decoy pair's amounts, the 3-minor-unit gap, and the "8/12 wrong, 0/12 wrong"
comparison.

**Visual — needed, this is the strongest visual moment in the pitch.** A
before/after: the four candidate settlements with the true pair highlighted
green and the decoy pair highlighted red/struck through, both sums shown
against the credit. If time allows, animate the naive solver picking red
first, then correct itself. This is the single idea worth spending render
budget on.

---

## 2:00–2:45 — Walk one real FX-drift attribution

**Say, over a real flagged case:** "Here's a settlement where the applied
conversion rate came in over 200 basis points from the FBIL reference rate
for that value date — well outside our tolerance band. The system doesn't
reject it — it decomposes the variance: is this a fee mismatch, a data-entry
error, or a genuinely bad rate? Here the arithmetic isolates it cleanly to
the rate itself, and it's flagged for review, not silently netted through.
And the tolerance band that decides 'inside' vs 'outside' isn't a number we
picked — it's derived from the labelled data: three standard deviations
over every legitimate rate in the training set, rounded down on purpose,
because a false clear costs more than an extra review case. On the harder
holdout set, that discipline mattered: a looser, more 'obvious' band would
have cleared two of three genuinely bad rates as benign."

**Visual — needed.** The FX attribution table from `reports/eval_report.md`
(NO_VARIANCE / BENIGN_FX_DRIFT / FLAGGED_FX_DRIFT / FEE_MISMATCH /
DATA_ENTRY_ERROR / UNRESOLVED, main vs. holdout columns) — six rows, clean,
renders well as a static table on screen while narrating over it.

---

## 2:45–4:00 — Close on the verified numbers, not claims

**Say:** "Here's what actually happened when we ran this against a labelled
ground-truth set, and a separate adversarial holdout we never tuned against:
zero percent false-match on both. Zero percent false-clear on both, once you
correctly separate a genuine tie — where two answers are mathematically
indistinguishable and the system honestly says so — from an actual miss.
That distinction matters enough that we built it into the metric itself,
not just the matcher.

And the zero isn't free. We don't just claim 'zero false-match' — we proved
the metric is sensitive to real error: deliberately corrupt 5% of known-good
matches, false-match rate moves to 5.26%. Corrupt 50%, it moves to 50%.
Monotonic, every time. If we hadn't done that, 'zero percent' would be an
unfalsifiable claim on an easy dataset. Now it's a number you can trust
because we tried to break it and it moved exactly the way it should."

**Visual — needed, this is the credibility payoff.** The mutation-test table
or a simple line chart: mutation rate on the x-axis, false-match rate on the
y-axis, a straight diagonal line. The visual argument ("the line is straight,
therefore the zero is real") lands faster than the sentence does.

---

## 4:00–4:45 — Tier 2, built proactively, and where it actually earned its cost

**Note for whoever records this: this beat replaced an earlier draft that
said "we deliberately didn't build the probabilistic/fuzzy stages." That's
no longer true — Tier 2 (Splink, then hybrid fuzzy text matching) has since
been built. Do not use the old line.**

**Say:** "Our own Tier 1 results gave no evidence of a recall gap that
needed fixing. We built the probabilistic and fuzzy-matching stages this
design calls for anyway — proactively, for genuine ML depth and robustness
against messier real-world text — and then built a forty-case adversarial
stress set specifically to test whether that bet paid off, engineered so
our deterministic matcher alone resolves zero of it.

Here's the honest result. Overall it takes that stress set from zero percent
resolved to fifty percent, with zero false matches introduced anywhere. But
that fifty percent isn't one number — it's earned unevenly, and the
unevenness is the actual finding. On OCR-style narration corruption:
complete recovery, eight for eight. On name transliteration: six of eight.
But on a legal entity name against a completely different trading name for
the same company — zero recovery. Zero out of eight. And that's not a bug
we're hiding — a legal name and a trading name share no characters in
common for a text-similarity model to find, and no embedding we trained on
this scale of data bridges that gap either. The system correctly declines
rather than guessing wrong, which is the same principle behind every
decision this engine makes."

*(Optional closing sentence if time allows — see cut-order below: "Two more
things we won't round up: our fee-mismatch and data-entry-error detection
are verified on exactly one real case each, not the roughly hundred-and-fifty
behind everything else we've shown you. We're saying that here, not hoping
you don't ask.")*

**Visual — needed, and this replaced the original plan.** A **horizontal bar
chart by failure category**, not a single before/after number: five bars
(OCR typos, transliteration, invoice-text mismatch, abbreviations, legal-vs-
trading-name), each showing percent resolved, sorted descending. The
legal-vs-trading-name bar visibly at zero, sitting at the bottom, is the
whole point — it has to be *visible*, not asterisked away in a footnote. A
single "0% → 50%" stat card was the original plan for this beat and would
actively mislead here: it makes an uneven, partial result look like a clean
uniform win, which is exactly the overclaim this project has avoided
everywhere else. Build the bar chart; do not fall back to the single number
under time pressure — cut narration instead (see below).

---

## 4:45–5:00 — Close

**Say:** "Deterministic-first, mathematically attributed, honestly measured,
and built for the part of reconciliation that happens the moment a payment
crosses a border. That's the pitch."

**Visual:** hold on the project name / one-line description card, nothing
else — don't end on a data table.

---

## Timing budget, if it's running long

Cut in this order, cheapest-to-lose first: (1) the optional closing sentence
on the Tier 2 beat (the FEE_MISMATCH/DATA_ENTRY_ERROR n=1 caveat) — drop it
before touching anything else; (2) the 2:45–4:00 mutation-test walkthrough
down to just the closing line + visual, no build-up; (3) the FX-drift
walkthrough (2:00–2:45) down to the table + one sentence. **Never cut the
subset-sum walkthrough (1:00–2:00) or the Tier 2 beat (4:00–4:45), and never
collapse the Tier 2 bar chart back down to a single "0%→50%" stat under time
pressure** — those two full beats, with their real visuals intact, are what
separate this from every other "we built a matcher" pitch in the track, and
the Tier 2 beat's entire value is the honest unevenness a single number
would erase.
