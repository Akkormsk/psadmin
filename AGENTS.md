# Project invariants

- The production database on Timeweb is the canonical assistant knowledge base.
- Local automated tests must not connect to or modify production data.
- Git contains code, migrations, and curated test fixtures, not production database dumps, credentials, customer documents, or supplier exports.
- The LLM interprets natural language. Calculations, validation, catalogue filtering, and ranking are performed by the backend.
- Confirmed administrator feedback and superseded knowledge must remain versioned and recoverable.
- Production changes are deployed through GitHub. Do not deploy or mutate Timeweb configuration without explicit authorization.
- Supplier integrations must not download images unless explicitly requested.

# Personal coding behavior

These rules apply to all coding work across projects on this machine. They supplement, not replace, project-level CLAUDE.md.

## 1. Think before coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity first

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Self-check: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical changes

Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

Test: every changed line should trace directly to the user's request.

## 4. Goal-driven execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan with explicit verification steps.

## 5. TDD and Test-first execution

Build your own evaluation loop. Fail first, then fix.

Before writing functional code:

- Whenever possible, write the test that reproduces the bug or validates the new feature first.
- Run the test to watch it fail before implementing the solution.
- Use local test execution as your primary feedback mechanism for agent self-evaluation.
- If a test cannot be written upfront, explicitly state the blocker before proceeding.

Test: You should observe a failing test state before you ever write the code to make it pass.

## 6. Code over comments

Self-explanatory code first. If comments are necessary, they must explain the code.
Comment context and constraints, not mechanics.

- Prioritize expressive naming and clean structure over explanatory comments.
- Only comment when the logic is inherently non-obvious or bound by external schemas.
- Focus comments strictly on the "why" of system quirks, business rules, and third-party restrictions.
- DO NOT explain language features, frameworks, or generic technology mechanics.

When tempted to write a comment, first try renaming or restructuring. Usually that removes the need.
Self-check: "If I delete this comment, does the code become harder to understand correctly?" If no, delete it.

---

Tradeoff: these rules bias toward caution over speed. For trivial tasks, use judgment.
