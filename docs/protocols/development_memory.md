# Development memory: verified findings and CI signals

Status: **CODE_ONLY** until the workflow is present on the GitHub default branch and an actual run is verified. No TEST/PROD deploy or DB schema changes.

## Purpose

Give the next Codex/Copilot chat a reliable answer to: *Was this case observed? Did it work? What failed? What was fixed? What remains unverified?* This is **not** a logging warehouse.

## Sources, with distinct meanings

1. **Raw evidence** remains in existing GitHub Actions runs, TEST database diagnostics, DocumentPreview, TenderComputeJob, and existing read-only audit/replay scripts. Never duplicate documents, AI replies, logs, or secrets in GitHub.
2. **Automatic CI signal**: `[CI signal]` Issue is one reusable alert per workflow/job on failure or timeout. It is **not** a proved bug. The collector updates an existing Issue rather than opening another for each run. It never reads job logs or runs tests. Do not promote to a bug solely from this signal.
3. **Curated finding**: an Issue title `[Finding][documents|eis|hermes|...] short description` authored/updated by the developer agent **only when backed by actual evidence**. Can record a successful capability, a confirmed limitation, an actual bug, or a resolved regression. Search first and update the existing Issue; do not create another for the same behavior.
4. **Domain protocol** remains the authority for accepted architecture, constraints and implementation status. Issues are a searchable case index, not competing specs.

## Required curated finding template

- **Question / observable behavior:** exact claim, scope, stage.
- **Result:** VERIFIED_SUCCESS / VERIFIED_FAILURE / FIXED_AND_REPLAYED / NOT_OBSERVABLE / UNKNOWN.
- **Environment & SHA:** exact tested version, TEST/CI/local.
- **Real case:** tender identifier or non-sensitive document format/size/structure; links to existing diagnosis/job only if accessible and privacy-safe.
- **Evidence:** exact test name, CI run, read-only query summary, or real replay. Distinguish input length (bytes/pages/tokens) from model limits; do not infer unmeasured token usage.
- **Cause:** confirmed cause, or 'not established'. Never infer it from an error string.
- **Fix / verification:** PR/commit; link to replay of the *same* scenario. A commit, a green deploy or absence of new errors alone is not verification.
- **Last checked:** date and SHA.
- **Applicability / next verification:** what the evidence does **not** prove.

For example, a request about whether an earlier model input exceeded the document limit must search curated findings and existing diagnostics. If there is no measured evidence, answer **NOT_OBSERVABLE**; never claim 'never happened' or 'not supported'. A green check on a small DOCX does not prove success on 90-item tenders.

## Agent workflow (required)

1. On starting work on document reading, EIS, Hermes or a neighboring feature, **search repository Issues including closed ones** for domain terms, technical symptom, and relevant tender IDs. Read domain protocol and exact evidence links. Do this before saying that a past case is unknown.
2. Distinguish **verified facts**, **open CI alerts**, **hypotheses**, and **unobservable questions** in the answer. Do not say "unknown" until the relevant Issues, protocol and saved diagnostics have been checked; state exactly what was checked.
3. After real investigations, append/refresh **one curated Issue per reproducible behavior**, including *successes and limitations* as well as failures. Existing Issue gets an update, not a duplicate. Do not create an Issue for routine green tests, every failure stack trace, or unsupported speculation.
4. On a fix, link code SHA and evidence, keep Issue open if replay cannot confirm the real scenario. When verified on the same case, close curated bug Issue and preserve the history. Reopen only on a newly verified regression.
5. Avoid persisting sensitive documents, full prompts, API credentials, personal data, huge logs or raw DB extracts.

## Automated collector

`.github/workflows/development-memory-ci.yml` is intentionally a **separate, asynchronous workflow**. It does not extend test/deploy jobs or Django request paths. It reads *completed* GitHub Actions job outcomes only. It cannot block or change the result of the originating workflow. Errors inside the collector stay within that collector.

GitHub's `workflow_run` trigger requires the workflow file on the repository **default branch**. Merging only into `test-server` will NOT activate event collection if the default is `main`. `workflow_dispatch` permits a safe single-run pilot before enabling automatic collection. Verify token's Issues write permission and a real failed run. Do **not** merge or enable on default branch without approval. No background application error instrumentation has been implemented; application diagnostics are source data to be inspected on demand.

## Noise and costs

Automatic Issues are grouped coarsely by workflow and job, meaning they are *signals to investigate*, not unique root causes. Only curated findings are authoritative for agent memory. Collector stores no per-test logs, no new PostgreSQL tables, no new scheduler, no AI/API calls outside GitHub Actions. GitHub API quotas and Issues permissions apply. If volume grows, tighten workflow allowlist rather than creating more detailed Issues.
