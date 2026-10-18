import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, ReminderSettings, Settings, SyncResult, Task
from coursedeck.knowledge import KnowledgeStore
from coursedeck.migrations import MigrationError, migrate, structure, validate_existing, versions
from coursedeck.recovery import RecoveryManager
from coursedeck.reminders import ReminderEngine, quiet_at


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 30, 16, tzinfo=UTC)]
    monkeypatch.setattr("coursedeck.db.now", lambda: clock[0])
    db = Database(tmp_path / "coursedeck.sqlite3")
    settings = Settings(reminders=ReminderSettings(enabled=True, quiet_enabled=False))
    db.save_settings(settings)
    engine = ReminderEngine(db, lambda: clock[0])

    def sync(*tasks, outcome=Outcome.SUCCESS):
        db.apply(
            "fixture",
            SyncResult(
                outcome=outcome,
                courses=[Course(provider="fixture", external_id="c", name="Math")],
                tasks=list(tasks),
            ),
            clock[0].isoformat(),
        )

    def task(key="one", hours=20, **kwargs):
        return Task(
            provider="fixture",
            external_id=key,
            course_external_id="c",
            title=key,
            due_at=clock[0] + timedelta(hours=hours),
            **kwargs,
        )

    return db, engine, clock, sync, task


def test_defaults_and_quiet_time_validation():
    settings = Settings()
    assert not settings.reminders.enabled
    assert settings.reminders.lead_24h and settings.reminders.lead_2h
    assert settings.reminders.quiet_start == "22:00"
    assert settings.reminders.quiet_end == "08:00"
    with pytest.raises(ValidationError):
        ReminderSettings(quiet_start="24:00")
    with pytest.raises(ValidationError):
        Settings(timezone="Not/AZone")


def test_thresholds_restart_and_concurrent_claims_are_deduplicated(setup):
    db, engine, clock, sync, task = setup
    sync(task())
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda _: engine.claim(), range(4)))
    assert sum(batch["count"] for batch in batches) == 1
    assert next(batch for batch in batches if batch["count"])["items"][0]["kinds"] == ["24h"]
    restarted = ReminderEngine(Database(db.path), lambda: clock[0])
    assert restarted.claim()["count"] == 0
    clock[0] += timedelta(hours=18)
    assert restarted.claim()["items"][0]["kinds"] == ["2h"]
    clock[0] += timedelta(minutes=6)
    assert restarted.claim()["count"] == 0


def test_completed_hidden_and_disabled_courses_do_not_remind(setup):
    db, engine, _, sync, task = setup
    tasks = [task("done", submission_status="submitted"), task("hidden"), task("localdone")]
    sync(*tasks)
    db.patch_local(tasks[1].id, {"hidden": True})
    db.patch_local(tasks[2].id, {"completion_override": "done"})
    assert engine.claim()["count"] == 0
    sync(task("open"))
    db.patch_course("fixture:c", {"disabled": True})
    assert engine.claim()["count"] == 0
    db.patch_course("fixture:c", {"disabled": False, "deleted": True})
    assert engine.claim()["count"] == 0


def test_unknown_status_and_stale_cache_are_cautious_not_completed(setup):
    db, engine, clock, sync, task = setup
    t = task(hours=50, submission_status="submitted")
    sync(t)
    clock[0] += timedelta(hours=30)
    sync(outcome=Outcome.NETWORK_ERROR)
    result = engine.claim()["items"][0]
    assert "Cached deadline" in result["warning"]
    assert "Submission status unknown" in result["warning"]
    assert not db.tasks()[0]["is_completed"]


def test_sleep_resume_collapses_missed_thresholds_and_never_replays_overdue(setup):
    _, engine, clock, sync, task = setup
    sync(*[task(str(i), hours=25) for i in range(10)], task("expired", hours=5))
    assert engine.claim()["count"] == 1  # Only the short deadline.
    clock[0] += timedelta(hours=24)
    batch = engine.claim()
    assert batch["count"] == 10
    assert all(item["kinds"] == ["2h"] for item in batch["items"])
    clock[0] += timedelta(minutes=6)
    assert engine.claim()["count"] == 0
    clock[0] += timedelta(days=1)
    assert engine.claim()["count"] == 0


def test_deadline_changes_reset_schedule_and_use_existing_change_events(setup):
    db, engine, clock, sync, task = setup
    t = task(hours=30)
    sync(t)
    assert engine.claim()["count"] == 0
    sync(t.model_copy(update={"due_at": clock[0] + timedelta(hours=28)}))
    assert engine.claim()["items"][0]["kinds"] == ["deadline_earlier"]
    clock[0] += timedelta(minutes=6)
    sync(t.model_copy(update={"due_at": clock[0] + timedelta(hours=20)}))
    assert set(engine.claim()["items"][0]["kinds"]) == {"24h", "deadline_earlier"}
    clock[0] += timedelta(minutes=6)
    sync(t.model_copy(update={"due_at": clock[0] + timedelta(hours=40)}))
    assert engine.claim()["count"] == 0
    clock[0] += timedelta(hours=17)
    assert engine.claim()["items"][0]["kinds"] == ["24h"]
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM task_changes WHERE kind='deadline_earlier'"
            ).fetchone()[0]
            == 2
        )


def test_effective_local_due_override_suppresses_irrelevant_source_change(setup):
    db, engine, clock, sync, task = setup
    t = task(hours=40)
    sync(t)
    db.patch_local(t.id, {"due_override": True, "due_at_override": clock[0] + timedelta(hours=50)})
    assert engine.claim()["count"] == 0
    sync(t.model_copy(update={"due_at": clock[0] + timedelta(hours=30)}))
    assert engine.claim()["count"] == 0
    db.patch_local(t.id, {"due_at_override": clock[0] + timedelta(hours=35)})
    assert engine.claim()["items"][0]["kinds"] == ["deadline_earlier"]


def test_snooze_is_persistent_and_rechecks_completion_visibility_and_due(setup):
    db, engine, clock, sync, task = setup
    tasks = [task(str(i)) for i in range(4)]
    sync(*tasks)
    assert engine.claim()["count"] == 4
    assert engine.snooze()["snoozed_until"]
    assert engine.status()["last_batch"]["count"] == 4
    db.patch_local(tasks[0].id, {"completion_override": "done"})
    db.patch_local(tasks[1].id, {"hidden": True})
    db.patch_local(
        tasks[2].id, {"due_override": True, "due_at_override": clock[0] + timedelta(days=3)}
    )
    restarted = ReminderEngine(Database(db.path), lambda: clock[0])
    clock[0] += timedelta(minutes=29)
    assert restarted.claim()["count"] == 0
    clock[0] += timedelta(minutes=1)
    batch = restarted.claim()
    assert [item["task_id"] for item in batch["items"]] == [tasks[3].id]
    assert batch["items"][0]["kinds"] == ["snoozed"]
    assert restarted.status()["snoozed_until"] is None


def test_quiet_hours_defer_one_summary_and_cooldown_collects_new_work(setup):
    db, engine, clock, sync, task = setup
    clock[0] = datetime(2026, 10, 1, 3, tzinfo=UTC)  # 23:00 New York
    settings = db.settings()
    settings.reminders.quiet_enabled = True
    db.save_settings(settings)
    sync(task())
    assert engine.claim()["count"] == 0
    clock[0] += timedelta(hours=9)
    assert engine.claim()["count"] == 1
    sync(task("two"))
    assert engine.claim()["count"] == 0
    clock[0] += timedelta(minutes=5)
    assert engine.claim()["count"] == 1


@pytest.mark.parametrize(
    "stamp,quiet",
    [
        ("2026-11-01T05:30:00+00:00", True),  # First 01:30, EDT
        ("2026-11-01T06:30:00+00:00", True),  # Repeated 01:30, EST
        ("2026-11-01T13:00:00+00:00", False),
        ("2026-03-08T06:59:00+00:00", True),
        ("2026-03-08T07:00:00+00:00", True),  # Skipped 02:00
        ("2026-03-08T12:00:00+00:00", False),
    ],
)
def test_quiet_hours_observe_dst(stamp, quiet):
    assert quiet_at(Settings(), datetime.fromisoformat(stamp)) == quiet


def test_equivalent_due_instants_do_not_reschedule_and_naive_is_not_guessed(setup):
    db, engine, clock, sync, task = setup
    t = task()
    sync(t)
    assert engine.claim()["count"] == 1
    clock[0] += timedelta(minutes=6)
    with db.connection() as connection:
        payload = t.model_dump(mode="json")
        payload["due_at"] = "2026-10-01T08:00:00-04:00"  # Original due: Oct 1 12 UTC
        connection.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(payload), t.id))
    assert engine.claim()["count"] == 0
    with db.connection() as connection:
        payload["due_at"] = "2026-10-01T12:00:00"
        connection.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(payload), t.id))
    assert engine.claim()["count"] == 0


def test_disable_and_reenable_do_not_replay_historical_deadline_changes(setup):
    db, engine, _, sync, task = setup
    sync(task(hours=50))
    engine.claim()
    settings = db.settings()
    settings.reminders.enabled = False
    db.save_settings(settings)
    sync(task(hours=40))
    assert engine.claim()["count"] == 0
    settings.reminders.enabled = True
    db.save_settings(settings)
    assert engine.claim()["count"] == 0


def remove_reminder_schema(db):
    with db.connection() as connection:
        connection.execute("DROP TABLE reminder_tasks")
        connection.execute("DROP TABLE reminder_runtime")
        connection.execute("DELETE FROM schema_migrations WHERE component='reminders'")


def test_preceding_schema_upgrade_and_paired_backup_preserve_reminders(setup, tmp_path):
    db, engine, _, sync, task = setup
    sync(task())
    batch = engine.claim()
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    manager = RecoveryManager(tmp_path, db, knowledge)
    backup = manager.create_backup()
    path = manager.folder(backup["id"]) / "coursedeck.sqlite3"
    copied = Database(path)
    assert ReminderEngine(copied).status()["last_batch"] == batch
    remove_reminder_schema(db)
    Database(db.path)
    Database(db.path)
    with db.connection() as connection:
        assert versions(connection)["reminders"] == 1
        assert len(db.tasks()) == 1
        connection.execute("UPDATE schema_migrations SET version=2 WHERE component='reminders'")
    with pytest.raises(MigrationError, match="newer"):
        Database(db.path)
    knowledge.close()


def test_failed_reminder_migration_rolls_back(setup, monkeypatch):
    from coursedeck import migrations

    db, _, _, _, _ = setup
    remove_reminder_schema(db)
    with db.connection() as connection:
        before = structure(connection)
        validate_existing(connection, "tasks")
        monkeypatch.setitem(
            migrations.SCHEMAS,
            "reminders",
            "CREATE TABLE reminder_tasks (task_id TEXT); INVALID SQL",
        )
        with pytest.raises(sqlite3.Error):
            migrate(connection, "tasks")
        assert structure(connection) == before
        assert "reminders" not in versions(connection)


def test_api_requires_local_header_and_obeys_recovery_barrier(tmp_path):
    app = create_app(tmp_path)
    app.state.db.save_settings(Settings(startup_sync=False))
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        assert client.get("/api/reminders/status").json()["enabled"] is False
        assert client.post("/api/reminders/claim").status_code == 403
        assert (
            client.post("/api/reminders/claim", headers={"x-coursedeck": "1"}).json()["count"] == 0
        )
        app.state.maintenance_gate.busy = True
        for path in ("claim", "snooze"):
            assert (
                client.post("/api/reminders/" + path, headers={"x-coursedeck": "1"}).status_code
                == 503
            )
        assert client.get("/api/reminders/status").status_code == 503
    finally:
        app.state.close_readers()


def test_fresh_deadline_advanced_into_past_alerts_once_without_overdue_backlog(setup):
    _, engine, clock, sync, task = setup
    t = task(hours=40)
    sync(t)
    assert engine.claim()["count"] == 0
    sync(t.model_copy(update={"due_at": clock[0] - timedelta(hours=1)}))
    assert engine.claim()["items"][0]["kinds"] == ["deadline_earlier"]
    clock[0] += timedelta(minutes=6)
    assert engine.claim()["count"] == 0


def test_old_earlier_event_after_long_sleep_does_not_replay_overdue(setup):
    _, engine, clock, sync, task = setup
    t = task(hours=40)
    sync(t)
    engine.claim()
    sync(t.model_copy(update={"due_at": clock[0] - timedelta(hours=1)}))
    clock[0] += timedelta(days=2)
    assert engine.claim()["count"] == 0


def test_pre_reminder_backup_prepare_and_restore(tmp_path):
    from coursedeck.recovery import digest, inspect_database, write_manifest

    db = Database(tmp_path / "coursedeck.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    manager = RecoveryManager(tmp_path, db, knowledge)
    backup = manager.create_backup()
    folder = manager.folder(backup["id"])
    copied = Database(folder / "coursedeck.sqlite3")
    remove_reminder_schema(copied)
    for name in manager.stores:
        path = folder / name
        backup["files"][name] = {
            "schema": inspect_database(path),
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        }
    write_manifest(folder / "manifest.json", backup)
    original = digest(copied.path)
    prepared = manager.prepare(backup["id"])
    assert prepared["valid"]
    assert original == digest(copied.path)
    manager.restore(prepared["id"])
    with db.connection() as connection:
        assert versions(connection)["reminders"] == 1
    assert ReminderEngine(db).status()["last_batch"] is None
    knowledge.close()


def test_deadline_advance_between_enabling_and_first_poll_is_not_lost(setup):
    db, engine, clock, sync, task = setup
    t = task(hours=50)
    sync(t)
    settings = db.settings()
    settings.reminders.enabled = False
    db.save_settings(settings)
    settings.reminders.enabled = True
    db.save_settings(settings)
    sync(t.model_copy(update={"due_at": clock[0] + timedelta(hours=40)}))
    assert engine.claim()["items"][0]["kinds"] == ["deadline_earlier"]


def test_custom_tasks_follow_local_due_and_completion_without_stale_warning(setup):
    from coursedeck.mail import CustomTaskInput, save_custom_task

    db, engine, clock, _, _ = setup
    save_custom_task(
        db, CustomTaskInput(title="Personal task", due_at=clock[0] + timedelta(hours=1))
    )
    batch = engine.claim()
    assert batch["count"] == 1 and batch["items"][0]["warning"] is None
    engine.snooze()
    db.patch_local(batch["items"][0]["task_id"], {"completion_override": "done"})
    clock[0] += timedelta(minutes=30)
    assert engine.claim()["count"] == 0


def test_individual_toggles_and_dst_threshold_use_elapsed_utc_hours(setup):
    db, engine, clock, sync, task = setup
    clock[0] = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    settings = db.settings()
    settings.reminders.lead_24h = False
    settings.reminders.deadline_earlier = False
    db.save_settings(settings)
    sync(task(hours=3))
    assert engine.claim()["count"] == 0
    clock[0] += timedelta(hours=1)  # Repeated 01:30 EST, now exactly 2 elapsed hours left
    assert engine.claim()["items"][0]["kinds"] == ["2h"]
    assert engine.claim()["count"] == 0
