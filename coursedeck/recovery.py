"""Validated paired local backups. Restore only inside the application's maintenance barrier."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .domain import now
from .migrations import MigrationError, migrate_copy, structure, validate_existing, versions

LEGACY_STAMP = r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z?"


class RecoveryError(ValueError):
    pass


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_database(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RecoveryError("Backup database integrity check failed.")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RecoveryError("Backup database contains broken references.")
            schema = db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            return hashlib.sha256(json.dumps(schema).encode()).hexdigest()
    except sqlite3.Error as exc:
        raise RecoveryError("Backup database could not be validated.") from exc


def write_manifest(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def schema_state(path, kind, *, allow_historical=False):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            validate_existing(connection, kind, allow_historical=allow_historical)
            return (
                structure(connection),
                versions(connection),
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
    except (sqlite3.Error, MigrationError) as exc:
        raise RecoveryError(str(exc)) from exc


def check_snapshot_sidecars(path):
    # A manifest certifies the SQLite file itself. Uncheckpointed writes in a
    # sidecar must never bypass its checksum during validation or copying.
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            raise RecoveryError("Backup has uncheckpointed changes; original files retained.")


class RecoveryManager:
    def __init__(self, data_dir, db, knowledge):
        self.root = (Path(data_dir) / "backups").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.stores = {"coursedeck.sqlite3": db, "knowledge.sqlite3": knowledge}
        self.journal = self.root / "restore-pending.json"

    def folder(self, backup_id):
        if not isinstance(backup_id, str) or not re.fullmatch(
            r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}", backup_id
        ):
            raise RecoveryError("Choose a backup from the local backup list.")
        folder = self.root / backup_id
        if folder.is_symlink() or folder.resolve().parent != self.root:
            raise RecoveryError("Backup path is outside the local backup directory.")
        return folder

    def create_backup(self, reason="manual"):
        if self.journal.exists():
            raise RecoveryError(
                "Recovery is pending. Restore a validated pair before creating a backup."
            )
        if reason not in {"manual", "pre_restore"}:
            raise RecoveryError("Unknown backup reason.")
        backup_id = now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        folder = self.folder(backup_id)
        folder.mkdir()
        files = {}
        for name, store in self.stores.items():
            path = folder / name
            store.backup(path)
            files[name] = {
                "sha256": digest(path),
                "schema": inspect_database(path),
                "bytes": path.stat().st_size,
            }
        manifest = {
            "id": backup_id,
            "version": 1,
            "created_at": now().isoformat(),
            "reason": reason,
            "files": files,
        }
        write_manifest(folder / "manifest.json", manifest)
        return manifest

    def preflight(self, backup_id, *, allow_upgrade=False):
        folder = self.folder(backup_id)
        manifest_path = folder / "manifest.json"
        try:
            if manifest_path.is_symlink() or manifest_path.resolve().parent != folder.resolve():
                raise RecoveryError("Backup manifest path is invalid.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or not isinstance(manifest.get("files"), dict)
                or not isinstance(manifest.get("created_at"), str)
                or not isinstance(manifest.get("reason"), str)
                or manifest["version"] != 1
                or manifest["id"] != backup_id
                or set(manifest["files"]) != set(self.stores)
            ):
                raise RecoveryError("Backup manifest is not a supported paired snapshot.")
            upgrade_required = False
            for name, store in self.stores.items():
                path = folder / name
                if path.is_symlink() or path.resolve().parent != folder.resolve():
                    raise RecoveryError("Backup database path is invalid.")
                check_snapshot_sidecars(path)
                info = manifest["files"][name]
                if (
                    not isinstance(info, dict)
                    or type(info.get("bytes")) is not int
                    or info["bytes"] <= 0
                    or not isinstance(info.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", info["sha256"])
                    or not isinstance(info.get("schema"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", info["schema"])
                ):
                    raise RecoveryError("Backup file metadata is invalid.")
                if path.stat().st_size != info["bytes"] or digest(path) != info["sha256"]:
                    raise RecoveryError("Backup checksum does not match. Nothing was restored.")
                schema = inspect_database(path)
                if schema != info["schema"]:
                    raise RecoveryError("Backup schema does not match its manifest.")
                kind = "tasks" if name == "coursedeck.sqlite3" else "knowledge"
                backup_state = schema_state(path, kind, allow_historical=True)
                current_state = schema_state(store.path, kind)
                if backup_state != current_state:
                    upgrade_required = True
                if upgrade_required and not allow_upgrade:
                    raise RecoveryError(
                        "Backup schema requires an upgrade. Prepare an upgraded copy first."
                    )
            return manifest | {
                "valid": not upgrade_required,
                "upgradable": upgrade_required,
                "replaces": [
                    "Tasks, courses, mail and local settings",
                    "Materials and chat history",
                ],
                "excludes": ["Browser sessions and credentials"],
            }
        except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Backup files or manifest could not be read.") from exc

    def legacy_pair(self, backup_id):
        if not isinstance(backup_id, str) or not re.fullmatch("legacy-" + LEGACY_STAMP, backup_id):
            raise RecoveryError("Choose a legacy backup from the local backup list.")
        stamp = backup_id.removeprefix("legacy-")
        paths = {}
        try:
            for name in self.stores:
                path = self.root / f"{Path(name).stem}-{stamp}.sqlite3"
                if path.is_symlink() or path.resolve().parent != self.root:
                    raise RecoveryError("Legacy backup path is outside the backup directory.")
                if not path.is_file():
                    raise RecoveryError(
                        "Legacy backup is missing its paired task or knowledge database."
                    )
                check_snapshot_sidecars(path)
                inspect_database(path)
                schema_state(
                    path,
                    "tasks" if name == "coursedeck.sqlite3" else "knowledge",
                    allow_historical=True,
                )
                paths[name] = path
        except OSError as exc:
            raise RecoveryError("Legacy backup files could not be read.") from exc
        return paths

    def import_legacy(self, backup_id):
        return self.prepare(backup_id)

    def prepare(self, backup_id):
        """Validate originals, then upgrade a new pair. Never modify a backup in place."""
        legacy = backup_id.startswith("legacy-")
        if legacy:
            paths = self.legacy_pair(backup_id)
            expected_hashes = {name: digest(path) for name, path in paths.items()}
        else:
            original = self.preflight(backup_id, allow_upgrade=True)
            paths = {name: self.folder(backup_id) / name for name in self.stores}
            expected_hashes = {name: info["sha256"] for name, info in original["files"].items()}
        imported_id = now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        folder = self.folder(imported_id)
        folder.mkdir()
        files = {}
        upgrades = {}
        try:
            for name, source_path in paths.items():
                check_snapshot_sidecars(source_path)
                if digest(source_path) != expected_hashes[name]:
                    raise RecoveryError(
                        "Backup changed during preparation; original files retained."
                    )
                destination = folder / name
                with closing(
                    sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
                ) as source:
                    with closing(sqlite3.connect(destination)) as target:
                        source.backup(target)
                check_snapshot_sidecars(source_path)
                if digest(source_path) != expected_hashes[name]:
                    raise RecoveryError(
                        "Backup changed during preparation; original files retained."
                    )
                kind = "tasks" if name == "coursedeck.sqlite3" else "knowledge"
                upgrades[name] = migrate_copy(destination, kind)
                if schema_state(destination, kind) != schema_state(self.stores[name].path, kind):
                    raise RecoveryError("Upgraded schema differs from this installation.")
                files[name] = {
                    "sha256": digest(destination),
                    "schema": inspect_database(destination),
                    "bytes": destination.stat().st_size,
                }
        except (MigrationError, sqlite3.Error, OSError) as exc:
            # No manifest is published for a partial preparation. Originals and live
            # databases remain untouched, and the incomplete folder stays visible.
            raise RecoveryError("Backup upgrade failed; original files were retained.") from exc
        manifest = {
            "id": imported_id,
            "version": 1,
            "created_at": now().isoformat(),
            "reason": "legacy_import" if legacy else "schema_upgrade",
            "original_id": backup_id,
            "upgrades": upgrades,
            "files": files,
        }
        write_manifest(folder / "manifest.json", manifest)
        return self.preflight(imported_id)

    def list_backups(self):
        result = []
        for folder in sorted(self.root.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            try:
                checked = self.preflight(folder.name, allow_upgrade=True)
                result.append(
                    {key: checked[key] for key in ("id", "created_at", "reason", "valid")}
                    | ({"upgradable": True} if checked["upgradable"] else {})
                )
            except RecoveryError as exc:
                result.append({"id": folder.name, "valid": False, "error": str(exc)})
        stamps = set()
        for path in self.root.glob("*.sqlite3"):
            match = re.fullmatch(
                r"(?:coursedeck|knowledge)-(" + LEGACY_STAMP + r")\.sqlite3", path.name
            )
            if match:
                stamps.add(match[1])
            else:
                result.append(
                    {
                        "id": path.name,
                        "valid": False,
                        "legacy": True,
                        "error": "Unrecognized legacy backup name; "
                        "a matching database pair is required.",
                    }
                )
        for stamp in sorted(stamps, reverse=True):
            entry = {"id": "legacy-" + stamp, "valid": False, "legacy": True}
            try:
                self.legacy_pair(entry["id"])
                entry["importable"] = True
            except RecoveryError as exc:
                entry["error"] = str(exc)
            result.append(entry)
        return {"backups": result, "interrupted_restore": self.journal.exists()}

    def _copy_pair(self, backup_id):
        folder = self.folder(backup_id)
        for name, store in self.stores.items():
            with closing(
                sqlite3.connect((folder / name).resolve().as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(store.path)) as target:
                    source.backup(target)

    def restore(self, backup_id):
        self.preflight(backup_id)  # Never trust an earlier UI preflight.
        pending = self.journal.exists()
        previous_id = None
        if pending:
            # The live databases may belong to different snapshots. Never certify them
            # as a new paired backup or overwrite the original recovery evidence.
            try:
                original = json.loads(self.journal.read_text(encoding="utf-8"))
                if isinstance(original, dict) and isinstance(original.get("rollback"), str):
                    self.preflight(original["rollback"])
                    previous_id = original["rollback"]
            except (RecoveryError, OSError, UnicodeError, json.JSONDecodeError):
                pass
        else:
            previous_id = self.create_backup("pre_restore")["id"]
            write_manifest(self.journal, {"target": backup_id, "rollback": previous_id})
        try:
            self._copy_pair(backup_id)
            for store in self.stores.values():
                inspect_database(store.path)
        except Exception as exc:
            try:
                if previous_id is None:
                    raise RecoveryError("No validated recovery backup is available.")
                self._copy_pair(previous_id)
                for store in self.stores.values():
                    inspect_database(store.path)
            except Exception:
                raise RecoveryError(
                    "Restore interrupted. Keep maintenance active and recover "
                    "the pre-restore backup before syncing."
                ) from exc
            if pending:
                raise RecoveryError(
                    "Restore failed. The original recovery backup was recovered, "
                    "but recovery remains pending; choose a validated pair and retry."
                ) from exc
            self.journal.unlink()
            raise RecoveryError(
                "Restore failed. Both databases were rolled back to the pre-restore backup."
            ) from exc
        self.journal.unlink()
        return {"restored": backup_id, "pre_restore_backup": previous_id}


class BackupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)


def build_recovery_router(manager, maintenance):
    router = APIRouter(prefix="/api/recovery")

    def checked(call, *args):
        try:
            return call(*args)
        except RecoveryError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/backups")
    async def backups():
        return checked(manager.list_backups)

    @router.post("/backups")
    async def backup():
        async with maintenance():
            return checked(manager.create_backup)

    @router.post("/preflight")
    async def preflight(value: BackupSelection):
        return checked(manager.preflight, value.id)

    @router.post("/import")
    async def import_legacy(value: BackupSelection):
        async with maintenance():
            return checked(manager.import_legacy, value.id)

    @router.post("/restore")
    async def restore(value: BackupSelection):
        async with maintenance():
            return checked(manager.restore, value.id)

    @router.post("/prepare")
    async def prepare(value: BackupSelection):
        async with maintenance():
            return checked(manager.prepare, value.id)

    return router
