# Database upgrades

`coursedeck/migrations.py` registers the task and knowledge database layouts. The
original task `PRAGMA user_version` remains at 7 after its existing historical data
migrations. Subsequent changes have per-component versions and timestamps in
`schema_migrations`, including associations, task edit history, and material
checkpoints.

Startup validates tables, columns, defaults, foreign keys, and indexes against
known layouts before applying registered additive changes. Unknown objects,
future versions, incomplete components, and broken references are rejected.
The additive upgrade and its version records share a SQLite savepoint. A failed
step rolls back both; it does not certify a partially upgraded schema.

## Supported backup upgrades

The first registered release supports known historical task schemas 1–7, including
known optional feature tables, and the previous unregistered knowledge layouts.
The latter may lack document metadata, message warnings, conversation task
context, chat activity, or material checkpoints. Missing values receive their
original empty/null defaults; the upgrade does not infer content or task state.
Older task schemas run their original data migrations on the copied database,
preserving course aliases, bindings, notes, and cached task visibility. Historical
data changes, additive schema changes, and their version records commit together.

An older paired backup is shown as **Upgrade needed** in Settings. **Prepare
upgraded copy** validates the original pair and writes a separate pair, applies
known upgrades there, checks integrity and references, and publishes a new
manifest only after both files match the running installation. Flat legacy pairs
use this same process through **Import backup**. Neither action restores data.

**Restore this backup** is available only after the prepared pair passes its
normal preflight. Restore keeps the maintenance barrier, current-data backup,
two-database rollback, and durable interrupted-restore journal. The original
backup files and manifest are retained unchanged. Uncheckpointed backup WAL or
journal files are rejected rather than bypassing the manifest checksums.

## Adding a migration

- Register a fixed, reviewed predecessor layout and its target version. Do not
  infer an accepted schema from whichever tables happen to exist on a user's
  machine, silently drop unknown objects, or merely increase a version number.
- Put schema changes in this module; retain the historical task data migrations
  until explicit replacement tests cover their transformations.
- Use additive transactional steps where possible. Any data transformation must
  preserve source task identity, local overrides, mailbox state, association
  decisions, history, and material/chat references.
- Test opening the preceding version, repeat startup, transaction failure, future
  version rejection, and preparation/restoration of a disposable paired backup.
  Never test restore against the user's active databases.

`tests/test_migrations.py` covers these boundaries, including preservation of
notes, mail rules, course aliases, confirmed links, task history, document bodies,
and cited chat messages. `tests/test_recovery.py` covers the restore journal and
paired rollback behavior.
