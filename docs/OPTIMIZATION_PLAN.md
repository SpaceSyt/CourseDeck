# Reliability and maintenance iteration

The user authorized all six improvements on 2026-09-11. Preserve the local-first
architecture, source identities, uncertainty, local edits and recoverable cached data.
Source access remains deterministic; model output cannot invent facts or expand write authority.

- [x] Shared task-state and field-observation contract, exported frontend types/rules.
- [x] One transactional local-edit service for UI and Chat, bounded explicit AI grants,
      version conflicts and persistent undo history.
- [x] Versioned task/knowledge migrations; validated upgrade of known older backup copies,
      preserving originals and rejecting unknown/future schemas.
- [x] Durable material checkpoints, source capability interface, scoped transient retries,
      cancellation/restart/recovery verification.
- [x] Incremental task indexing and relation invalidation, conditional workspace fetching,
      no missed updates from mail/course/local changes or restored databases.
- [x] Smaller frontend modules, repeatable generated contracts and CI for backend/frontend,
      migration and isolated browser workflows.

## Verification

Completed locally on 2026-09-11 (America/New_York):

- `python -m pytest -q`: 458 passed. Two existing dependency deprecation warnings remain.
- `ruff check coursedeck tests scripts`, `ruff format --check coursedeck tests scripts`,
  `python scripts/export_task_contract.py --check`, and `git diff --check`: passed.
- Frontend `npm test`: 17 passed; `npm run build` and `npm run format:check`: passed.
- Eight isolated Chromium workflows passed: main UI, Chat, streamed Chat, material search,
  changes, associations, mail rules, and recovery. Main UI includes save/undo of a note;
  recovery includes upgrading a copied old backup and interrupted-restore startup recovery.
- Windows recovery tests exposed a process-lifecycle bug: terminating a venv launcher did
  not mean its backend had exited. Smoke helpers now wait for every descendant's Windows
  process handle before touching SQLite again. No delay/retry masks database errors.
- Stable 25-call relation and task-index runs perform no inventory/task reload or writes.
  Updating one task completion writes one indexed document. Searching 500 documents with
  another query reuses their normalized text without changing matching or freshness rules.
- External commits, restored/replaced databases, course changes, mail rules, local overrides,
  and association decisions invalidate caches. Frontend polls are serialized and use ETags;
  failed requests retain content, and a mutation during a read schedules another read.

Live validation used a paired backup before updating the service. An isolated upgrade of
that real backup preserved every original row. After restart, all 75 task IDs, 64 mail IDs,
153 original document IDs, local notes/preferences/rules, chat messages and API configuration
were preserved; automatic reading added another document. Both databases passed SQLite
integrity and foreign-key checks. A read-only Chromium check of the running service verified
course/settings/task-history rendering, mobile width, heartbeat and HTTP 304 polling with
no JavaScript errors or write requests. The running service was restarted with final code.

Source coverage is not newly certified by this iteration. Gradescope and WebAssign reported
success in the observed run; Classroom and Brightspace still honestly reported partial
coverage, including material limitations. Parser selectors were not changed, and no model
was called for validation. Checkpoints/retries improve recovery, not proof of completeness.

GitHub Actions configuration is present for Windows/Linux Python checks, frontend checks,
generated contracts and isolated Chromium workflows. Those commands were verified locally;
the hosted workflow has not been run or published by this iteration.

## Coordination

Parallel work was resumed after an interruption, integrated by the main task and verified
together. See `DATABASE_MIGRATIONS.md` and `CHAT_TOOLS.md` for extension and edit contracts.
