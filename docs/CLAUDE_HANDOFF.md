# Claude handoff — Incoming Tender Understanding

## Read this first

Work in `Akkormsk/psadmin`, branch `codex/calculation-v2-foundation`.
Test is deployed from that branch only; production remains on `main` and must
not be changed. Before editing, read `AGENTS.md`, `docs/PSADMIN.md` and this
file, then verify branch, HEAD, Git status and test deployment.

Do **not** replace the existing assistant/feedback/provider architecture or
the global assistant drawer. The separate `docs/claude-production-base-prompt.md`
is owned by parallel assistant work; preserve it unless its owner explicitly
asks otherwise.

## Current test release

- Test commit: `8557f88c6f10593050573ec45caeaf5a8443c5ca`.
- Test URL: `https://test.admin.psodin.ru/tender-selection/`.
- Production was not modified.
- Focused tests passed before release.

## What is implemented

The V2 pre-routing intake now has one evidence-only document-composition pass:

1. Notification rows are the baseline (`TenderSourceItem`).
2. Every cached document is given an equal bounded excerpt; filenames and
   keyword lists never decide whether a document is read.
3. One `gemini/gemini-3.1-flash-lite` call compares every document with every
   notification row. It may return only explicit `enrich`, `split` or
   `component` findings, each tied to `parent_source_item_id`, document URL,
   section and short evidence.
4. Findings are inactive `document_proposal` rows. The tender pauses for owner
   review; accepting applies the documented structure, rejecting removes it.
   No route calculation starts before that choice.
5. No single-item heuristic remains. Multiple notification rows can contain
   independent sets/components.

The default has one network attempt and one JSON attempt, so an invalid JSON
response cannot silently cause a second paid call. Flash Lite is the default.
The bounded payload and output cap are designed for an average cost at or below
1 ruble per tender; this is a diagnostic target, not a hard cancellation of a
legitimately large tender. An unknown-price model is blocked safely.

## Evidence from controlled tests

Two real Gateway comparisons spent **1.1066 rubles** of the owner-approved
20-ruble comparison budget; **18.8934 rubles remain**.

- A real tender with 51 notification rows and 8 cached documents: Flash Lite
  returned one evidenced enrichment for 0.3847 rubles; GPT-4.1 mini returned
  two for 0.5258 rubles. Both mappings were structurally valid.
- `tenders/fixtures/document_composition_benchmark.json`: both models correctly
  decomposed a set into a cup ×1 and metal pen ×2 per set, while enriching a
  separate notebook. Flash Lite cost 0.1093 rubles; GPT-4.1 mini 0.0868.

Use `manage.py benchmark_document_composition` only deliberately. It is
read-only, takes `--fixture` or a tender id, allows only a total cap up to
20 rubles, and never mutates tender records or runs routes.

## Required next work: finish intake quality on test

Goal: an agent must decide semantic relevance after title triage and before
routing. A tender whose title looks relevant may still contain unsuitable goods
(for example, a generic souvenir tender containing a plaster bust). This must
be semantic evidence-based reasoning by the model, **not** a hardcoded product
or document-name list.

Implement the smallest extension of the existing V2 intake flow:

- Use the same bounded Flash Lite document pass, or at most one additional
  bounded call only if evaluation shows a combined output unreliable.
- Return an explicit decision: `fit`, `not_fit`, or `needs_review`, with short
  evidence tied to actual notification/document text.
- `not_fit` must not silently delete or dismiss a tender. It must be visible to
  the owner with a reason; uncertain evidence must go to review.
- Keep all supplied documents eligible. “ТЗ”, “ООЗ” and “НМЦК” are hints to
  the model, never filename filters.
- Preserve both kinds of document findings: components/splits and additional
  documented requirements for already-existing rows.
- The owner view must make the delta unambiguous: notification baseline →
  documented findings → result after accept/reject.
- Do not begin routing, catalog warming or calculation aggregation in this
  milestone.

Validate with 5–10 real **test** tenders containing explicit nested goods,
plus negative semantic examples. Spend from the remaining comparison budget
only through the capped benchmark command; do not perform a mass run.

## Future production recheck — intentionally disabled

`CALCULATION_V2_EXISTING_RECHECK_ENABLED` is implemented and defaults to
false. `manage.py queue_existing_v2_recheck --limit N --apply` is the future
opt-in bounded queue for current Incoming and Kanban tenders. It excludes
dismissed/archived records and deduplicates `v2-existing-recheck` jobs. Do not
enable it in test or production until intake quality is accepted.

## Safety and deployment

- No production changes, secrets, destructive migrations, forced Git updates
  or unbounded AI/backfill.
- Keep V1 routes/calculator and existing provider/assistant mechanisms live.
- Use focused tests; deploy only the isolated test VDS after tests pass.
- Commit logical units and update this handoff with verified SHA/results.

## Copy/paste prompt

```text
Read AGENTS.md, docs/PSADMIN.md and docs/CLAUDE_HANDOFF.md. Work only in
codex/calculation-v2-foundation. Continue the V2 incoming-tender understanding
milestone: add semantic fit/not-fit/review after title triage and before
routing, using the existing bounded evidence-only document pipeline. Never
hardcode document names or product keywords; preserve owner review and do not
start routing/cache warming. Validate on a small explicit test corpus under the
remaining capped AI comparison budget. Keep future all-existing-tender recheck
behind a disabled opt-in flag and command. Test, commit, deploy only test, and
update this handoff. Do not modify production or the parallel assistant prompt.
```
