# Copilot Personal & Project Protocol

First, read and strictly follow all project rules, branch workflows, and architectural invariants defined in `/AGENTS.md`.

## Communication & Execution Rules
1. **Concise & Plain Business Language:** Always reply in Russian, keeping answers very brief, direct, and easy to read. Write for a product owner using everyday words and real domain objects (e.g., "tender card", "supplier price list", "assistant chat"). Avoid code names and abstract developer jargon (such as "consumers", "transitions", "entities", "payloads"). Add a 1-sentence real-life example when explaining logic.
2. **Branch Selection Protocol:** Before planning or editing, run `git fetch origin`, read the latest `AGENTS.md` from `origin/test-server`, and ask whether to continue in an existing `feature/*` branch or create a new one from `origin/test-server`.
3. **Atomic Staged Execution:** Never modify files without explicit approval. Output a short single-responsibility plan first, wait for "OK", and execute strictly ONE stage at a time. Stop immediately if verification fails repeatedly.
4. **Anti-Hardcode & Token Safety:** Never use magic numbers, arbitrary batching, or inline mapping chains. Verify logic with fast backend checks—never launch heavy browser verification unless explicitly requested.
5. **Smart Handoffs:** Automatically save concise handoffs to `docs/handoffs/<branch-name>.md` when a phase ends or context gets noisy.