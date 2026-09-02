# Design Description: A Cross-Border-Aware, Three-Way Settlement Reconciliation Engine
### Razorpay AI Buildathon 2026 — Track 04, AI Finance Controller

---

## 1. What this is, in one paragraph

A reconciliation engine that ties three sources of truth — Razorpay's settlement report, the merchant's bank statement, and the merchant's own invoice/order ledger — down to the paisa, using a graduated cascade of matching techniques that only escalates to a language model when deterministic and statistical methods genuinely can't resolve a case. On top of that domestic-grade core sits the layer no other visible submission has built: a cross-border intelligence module that validates whether an applied FX rate was reasonable, decomposes unexplained variance into FX drift versus fee mismatch versus data-entry error, and tracks each export receipt against its RBI EDPMS shipping-bill closure obligation. The system reports false-match rate — not match rate — as its headline number, because a wrong match that silently corrupts the books is a worse failure than an honest "I don't know."

## 2. The problem, stated the way a finance operator actually experiences it

A bank doesn't credit a merchant per payment — it credits one net lump sum per transfer, sweeping several settlements under a single UTR, net of fees, GST, refunds, and adjustments. So "which settlement is this bank credit for?" often has no single answer; the answerable question is "which *subset* of settlements does this credit cover?" — a search problem, not a lookup. Layer cross-border trade on top of that and it gets worse: the remitter's name arrives mangled through SWIFT narration, the FX rate applied is a fact you're handed rather than a fact you can trust, and every export receipt carries a silent regulatory clock — RBI requires it be matched to its shipping bill and marked realised within FEMA's timeline, or the exporter gets caution-listed. Today this is done by hand, in spreadsheets, by people who are good at noticing when three numbers that should agree don't.

## 3. System overview — how data moves through it

Three files or feeds come in: the Razorpay settlement/payment export (with its `conversion_rate` and `base_amount` fields intact for non-USD transactions), the bank statement (camt.053, MT940, or CSV), and the invoice/order ledger. Everything is normalized into a canonical record the moment it's ingested, and from that point forward **every monetary value is an integer minor unit or a `Decimal` — never a float.** This sounds like a footnote; it isn't. Several of the strongest competing submissions call this out explicitly as a design principle, because float arithmetic silently breaks equality checks in exactly the place a reconciliation engine can least afford it.

From there, records flow through a matching cascade with five stages, each one justified by a specific failure mode the previous stage can't catch. Anything that survives all five stages without a confident resolution is not guessed — it's routed to a human, by design.

## 4. The matching cascade

**Stage 1 — Deterministic exact match.** Reference ID or UTR plus amount within a tight tolerance band. This is the boring, high-volume, near-zero-risk majority of records. Highest priority because it's fully auditable and carries essentially no false-match risk.

**Stage 2 — Bounded subset-sum reconciliation.** This is the stage a naive two-pass design misses entirely, and it's the single most important correction this design makes relative to an earlier, simpler plan. When one bank credit doesn't match any single settlement, the right question isn't "fuzzy match this" — it's "does some subset of open settlements sum to this credit, within tolerance?" A bounded combinatorial solver answers that. This is exactly the technique the strongest visible competing submissions in this track have converged on independently, because it's what the actual shape of Razorpay's settlement sweeping behavior demands.

**Stage 3 — Probabilistic record linkage (Fellegi-Sunter, via Splink).** For records that agree on several *partial* signals — close amount, right week, similar counterparty name — but no single exact key, a Fellegi-Sunter model gives a calibrated, per-field-weighted match probability rather than an opaque score. This is stronger than jumping straight to embeddings because it's explainable to an auditor: you can point at exactly which fields agreed and by how much.

**Stage 4 — Hybrid fuzzy text matching, for the genuinely messy cases.** This is the layer that's specific to the cross-border problem and that no domestic-only competitor needs: unstructured remitter narration from a SWIFT field, or an invoice description that doesn't match the settlement text verbatim. The primary signal here is character-n-gram TF-IDF and Jaro-Winkler similarity — not dense embeddings alone, because embeddings blur exactly the numeric and identifier tokens (amounts, reference codes) that matter most in financial text. Dense vectors, pulled from FAISS or Chroma, are fused in via reciprocal rank fusion only as a secondary signal, to catch genuine semantic divergence like a legal name versus a trading name.

**Stage 5 — Calibrated abstention.** Every match, at every stage, carries a calibrated confidence score. Above a threshold set from labelled data, it auto-matches. Below a second threshold, it's a clean miss. In between, it's queued for a human, with the top candidate matches and their scores attached — not silently resolved either way. The threshold is chosen to hold false-match rate under a pre-declared budget, because in finance a wrong match is a worse error than an unresolved one.

## 5. The cross-border intelligence layer — the actual differentiator

This is the module that sits on top of the domestic-grade core and is where this design earns its place, because the search evidence is unambiguous: nobody else in this track has built it.

**FX-tolerance validation.** Every non-USD settlement's applied conversion rate is checked against the FBIL daily reference rate for that value date, within a band calibrated from labelled data to account for legitimate interbank spread and provider markup. A rate outside the band isn't automatically wrong — it's flagged for the decomposition step below.

**Variance decomposition.** For any settlement where the net amount doesn't match the expected gross, the system solves for which term explains the gap: `net = gross − MDR − GST_on_MDR − FX_spread − refund_adjustments`. The residual is attributed mathematically, not guessed — a benign FX drift within tolerance, a fee miscalculation, or a genuine data-entry error each leave a different arithmetic signature.

**Refund FX asymmetry.** A refund converts at its own FX event, not the original capture's rate — so even a "full" refund doesn't net to zero in the original currency, and the system reconciles it against the refund's own conversion rather than flagging it as a mismatch.

**Where the remitter narration actually comes from.** Stage 4's fuzzy matcher isn't guessing at unstructured text in the abstract — it operates on specific fields. For SWIFT MT103 messages, the ordering customer sits in field 50a, the beneficiary in field 59, free-text remittance information in field 70, and charge details in field 71A, with value date/currency/amount packed into field 32A. For the ISO 20022 equivalent (camt.053, which MT940 is migrating toward), the same information arrives in structured Debtor/Creditor blocks and a dedicated `RmtInf` element — already cleaner than MT free text, which is part of why this fuzzy-matching burden will shrink over time as CBPR+ migration completes, but won't disappear for the existing statement base or for invoice-side free text the counterparty writes by hand. The parser has to target field 70 and the Debtor/Creditor blocks specifically, not just tokenize a whole statement line.

**This is implemented, not just described.** The synthetic bank-statement generator produces actual MT103-formatted text and camt.053 XML — not flat CSVs standing in for them — and the ingestion layer runs a real field-level parser against both formats before anything reaches the matching cascade. This is the cheapest high-visibility differentiator available: it costs little beyond building the synthetic-data generator you need anyway, and it's the difference between a demo that matches two CSVs and one that parses the message formats a bank actually sends.

**EDPMS/shipping-bill linkage.** Every export receipt is tracked against its shipping bill and purpose code, with an aging view against the realisation deadline, because this is the actual regulatory stake behind the whole exercise — an unreconciled shipping bill open too long gets the exporter caution-listed by RBI, not just annoyed.

**Nostro/vostro timing awareness.** Cross-border settlements land T+2 to T+7. Records inside that expected window are held in a `TIMING_PENDING` state, not flagged as breaks — a naive matcher that doesn't know this over-reports exceptions that are just waiting on the clock.

## 6. Exception taxonomy and root-cause attribution

Nothing lands in a single opaque "unreconciled" bucket. Every unresolved record is classified into a named cause — benign FX drift, flagged FX drift, fee mismatch, missing sender information, timing-pending, partial payment, refund FX asymmetry, purpose-code mismatch, open EDPMS linkage, data-entry error, or genuinely needs manual review — derived from which stage of the cascade it fell out of and what the variance decomposition found. The category is decided by rules and arithmetic, never by the language model.

## 7. The language model's one job

The LLM writes a one-line, plain-English explanation of an already-decided exception. It is never given latitude to decide whether two records match, and it has no code path through which it could alter a financial figure — it's handed a structured verdict (category, amounts, the fields that did or didn't agree) and asked only to phrase it for a human reader. This boundary exists because LLMs are demonstrably miscalibrated on this exact kind of task and will confidently invent a match that isn't there if given the chance to decide rather than describe.

## 8. Audit trail

Every decision — which stage resolved it, the confidence score, the fields compared, and the timestamp — is written to an append-only, hash-chained log. This isn't decoration; every strong competing submission treats it as load-bearing, because "the system matched it" is not itself a control — the control is the logic plus a reviewable trail of why. The target substrate is TigerBeetle, a purpose-built double-entry ledger database with deterministic execution and enforced balance invariants — not just a hash-chained Postgres table standing in for one. No competing submission examined uses a purpose-built ledger store, which makes this a genuine infrastructure differentiator if the client tooling cooperates. It's attempted only after Stages 1–5 and the FX layer are working, on a Postgres append-only journal as the fallback if TigerBeetle's setup consumes time the core logic needs instead.

## 9. Evaluation methodology

Reporting leads with **false-match rate and false-clear rate**, not raw match rate, because those are the two failure directions that actually cost money or trust. Numbers are measured against a machine-generated ground-truth set with deliberately injected, labelled defects — spanning clean matches, subset-sum bundles, FX-drift cases both benign and flagged, missing-remitter cases, partial payments, and refund asymmetries — evaluated on both a main set and a held-out adversarial set the matching logic wasn't tuned against, at multiple record-count scales to also report throughput. **The harness itself is mutation-tested**, not just the matcher: known-good links are deliberately corrupted and the false-match metric is confirmed to actually move, so a "0% false-match" claim is demonstrated to be sensitive to real error rather than a vacuous zero on an easy dataset. This is the direct, pre-emptive answer to the question a payments-literate judge will ask first.

## 10. Why this beats a generic reconciliation submission

Every part of the domestic-grade core — deterministic-first matching, LLM confined to explanation, an honest categorized exception list, false-match rate as the headline metric — is table stakes in this track now; multiple independent teams converged on nearly identical language for exactly that architecture. What isn't table stakes, and what no examined submission has built, is the FX-tolerance validator, the variance decomposition, the EDPMS/shipping-bill regulatory linkage, and the fuzzy matching layer built specifically for cross-border remitter narration rather than clean domestic references. The pitch isn't "we built a reconciliation engine" — it's "we built the reconciliation engine that handles what happens the moment a payment crosses a border," on top of a core that's as rigorous as the best domestic entries.

## 11. Mapping to existing tooling

Splink for the probabilistic pass; FAISS or ChromaDB for the dense half of the hybrid fuzzy matcher; a lightweight LangChain-orchestrated call for the bounded explanation step; FastAPI as the service layer; Isolation Forest as an optional secondary anomaly signal on the residual "unexplained variance" bucket, consistent with the anomaly-detection pattern already used elsewhere in this line of work; Optuna if the FX tolerance band or confidence thresholds are tuned rather than hand-set.

**A lightweight cross-encoder pass, benchmarked honestly, as a stretch addition.** The synthetic dataset already generates labelled ground-truth pairs for evaluation — that same data doubles as training or prompting material for a small pretrained cross-encoder tested against the residual Splink+hybrid can't resolve. This is Ditto's core idea (deep pretrained-LM entity matching) at a scale that fits a hackathon, rather than the full VLDB-benchmark infrastructure. Built only after the core cascade works, and reported honestly either way: the research literature suggests dense/LM methods may not beat classical and probabilistic methods on short structured financial strings, and an ablation showing no lift — measured, not asserted — is as credible a result as one showing a gain.

**Correctly excluded, not revisited:** Magellan/py_entitymatching, dedupe, and febrl solve the same record-linkage problem Splink already solves, with less mature tooling for this use case. A second or third redundant framework doesn't demonstrate additional capability to a judge — it's the same idea restated.

## 12. Build sequencing, given hackathon time, in explicit tiers

**Tier 1 — must build, in this order:** the deterministic pass, the subset-sum solver, and the FX decomposition validator against real synthetic data with injected ground truth. Generate that synthetic data in actual MT103 and camt.053 formats from the start, not CSVs — the parser and the data model are the same effort either way, and building against realistic formats from day one avoids a costly rewrite later. Build the adversarial holdout split and the harness mutation test alongside the evaluation code, since it's the same code path as the main evaluation, just exercised twice. Write the citation trail into the README as each design decision is made, not retroactively — it's cheaper to record a rationale when it's fresh than to reconstruct it before submission.

**Tier 2 — add next, only once Tier 1 is demoable end to end:** the Fellegi-Sunter probabilistic pass, if the deterministic-plus-subset-sum stages leave a real recall gap on the synthetic set; the hybrid fuzzy text layer, leading with character-n-gram TF-IDF and adding the dense-embedding half only if TF-IDF alone doesn't close the residual.

**Tier 3 — attempt only with time to spare, and only after Tier 1 and 2 are solid:** TigerBeetle as the actual ledger substrate, with a Postgres append-only journal as the fallback if setup time runs long; a lightweight cross-encoder ablation on the residual, reported honestly whether or not it beats Splink+hybrid.

**Not revisited:** Magellan/py_entitymatching, dedupe, febrl — redundant with Splink, no marginal payoff for the time cost.
