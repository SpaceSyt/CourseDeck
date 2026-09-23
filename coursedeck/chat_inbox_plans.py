"""Reviewable mail deletion plans; execution requires an explicit UI confirmation."""

import asyncio
import hashlib
import json
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .mail import MailStore

FILTERS = {"query", "sender", "subject", "before", "after", "include_deleted", "include_ignored"}
SUCCESS = {"deleted", "trashed", "already_trashed"}


class MailDeletionPlans:
    def __init__(self, service):
        self.service = service
        self.plans = {}

    def get(self, key):
        plan = self.plans.get(key)
        if plan is None or (monotonic() - plan["created"] > 3600 and not plan["lock"].locked()):
            self.plans.pop(key, None)
            raise HTTPException(404, "This deletion plan expired; ask Chat to preview it again")
        return plan

    @staticmethod
    def fingerprint(conn, ids):
        rows = conn.execute(
            "SELECT id,payload,local_payload FROM mail_messages WHERE id IN ("
            + ",".join("?" for _ in ids)
            + ") ORDER BY id",
            ids,
        ).fetchall()
        return hashlib.sha256(json.dumps([list(row) for row in rows]).encode()).hexdigest()

    def create(self, inbox, conversation_id, args):
        if not isinstance(args, dict) or args.keys() - (FILTERS | {"mail_ids", "destination"}):
            raise ValueError("Invalid deletion preview parameters")
        destination = args.get("destination")
        if destination not in {"local", "gmail"}:
            raise ValueError("Choose local Inbox deletion or Gmail Trash")
        ids = args.get("mail_ids")
        filters = {key: value for key, value in args.items() if key in FILTERS}
        if ids is not None:
            if (
                filters
                or not isinstance(ids, list)
                or not ids
                or len(ids) > 2000
                or any(not isinstance(key, str) or key not in inbox.seen for key in ids)
            ):
                raise ValueError("Choose IDs returned by search_inbox in this turn, or use filters")
            filters = {"include_deleted": True, "include_ignored": True}
        with self.service.db.connection() as conn:
            conn.execute("BEGIN")
            # Internal selection must not grant read access as if the model had searched these IDs.
            candidates, missing_dates, _ = inbox._matching(filters, connection=conn)
            if ids is not None:
                wanted = set(ids)
                candidates = [mail for mail in candidates if mail["id"] in wanted]
                if len(candidates) != len(wanted):
                    raise ValueError("Some selected emails are no longer available in this scope")
            total = len(candidates)
            limit = 20 if destination == "gmail" else 2000
            candidates = candidates[:limit]
            if not candidates:
                raise ValueError("No cached emails match; no deletion plan was created")
            ids = [mail["id"] for mail in candidates]
            version = self.fingerprint(conn, ids)
        account = None
        if destination == "gmail":
            gmail = self.service.gmail
            if gmail is None or gmail.status()["status"] != "connected":
                raise ValueError("Connect Gmail before preparing a Gmail Trash operation")
            account = self.service.db.state("gmail").get("account")
            if not account:
                raise ValueError("The connected Gmail account could not be verified")
            if any(not mail["id"].startswith("gmail:") for mail in candidates):
                raise ValueError("Only cached Gmail conversations can be moved to Gmail Trash")
        for key, old in list(self.plans.items()):
            if monotonic() - old["created"] > 3600 and not old["lock"].locked():
                self.plans.pop(key)
        if len(self.plans) >= 64:
            raise ValueError("Too many deletion plans; finish existing plans or retry later")
        key = uuid4().hex
        description = (
            "; ".join(
                f"{field}: {value}"
                for field, value in filters.items()
                if field not in {"include_deleted", "include_ignored"}
            )
            or "Selected cached emails"
        )
        if "before" in filters or "after" in filters:
            description += "; timezone: " + self.service.db.settings().timezone
        excluded = []
        if not filters.get("include_deleted"):
            excluded.append("locally deleted")
        if not filters.get("include_ignored"):
            excluded.append("ignored")
        if excluded:
            description += "; excludes " + " and ".join(excluded) + " mail"
        public = {
            "id": key,
            "version": version,
            "destination": destination,
            "status": "pending",
            "matched": len(ids),
            "total_matched": total,
            "remaining_count": total - len(ids),
            "missing_date_count": missing_dates
            if "before" in filters or "after" in filters
            else 0,
            "description": description,
            "messages": [
                {field: mail.get(field) for field in ("id", "subject", "sender", "received_at")}
                for mail in candidates
            ],
        }
        self.plans[key] = {
            "public": public,
            "ids": ids,
            "version": version,
            "account": account,
            "inbox": inbox,
            "filters": filters,
            "conversation_id": conversation_id,
            "created": monotonic(),
            "lock": asyncio.Lock(),
            "receipt": public | {"messages": public["messages"][:20], "has_more": len(ids) > 20},
        }
        return self.plans[key]["receipt"]

    def validate(self, plan, conn):
        # Re-evaluate course bindings and filters; an old preview cannot authorize new matches.
        current = {
            mail["id"]: mail for mail in plan["inbox"].matching(plan["filters"], connection=conn)
        }
        if (
            not set(plan["ids"]).issubset(current)
            or self.fingerprint(conn, plan["ids"]) != plan["version"]
        ):
            raise HTTPException(409, "Emails changed; ask Chat to create a new deletion preview")
        return [current[key] for key in plan["ids"]]

    def persist_receipt(self, plan):
        # If Chat is still streaming, its Activity references this same public object.
        # Otherwise update the already-saved receipt so reloads retain the operation outcome.
        plan["receipt"].update(
            {key: value for key, value in plan["public"].items() if key != "messages"}
        )
        with self.service.store.connection() as conn:
            for row in conn.execute(
                "SELECT a.message_id,a.payload FROM chat_activity a JOIN messages m "
                "ON m.id=a.message_id WHERE m.conversation_id=?",
                (plan["conversation_id"],),
            ).fetchall():
                activity = json.loads(row["payload"])
                changed = False
                for event in activity:
                    if (event.get("mail_deletion_plan") or {}).get("id") == plan["public"]["id"]:
                        event["mail_deletion_plan"] = plan["receipt"]
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE chat_activity SET payload=? WHERE message_id=?",
                        (json.dumps(activity, ensure_ascii=False), row["message_id"]),
                    )

    async def apply(self, key, expected_version):
        plan = self.get(key)
        if expected_version != plan["public"]["version"]:
            raise HTTPException(409, "The deletion preview changed; reload it")
        if plan["lock"].locked():
            raise HTTPException(409, "This deletion is already running; refresh its status")
        async with plan["lock"]:
            public = plan["public"]
            if public["status"] != "pending":
                return public
            with self.service.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE" if public["destination"] == "local" else "BEGIN")
                items = self.validate(plan, conn)
                if public["destination"] == "local":
                    results = []
                    for item in items:
                        MailStore.patch_local(conn, item["id"], {"deleted": True})
                        results.append(
                            {"id": item["id"], "status": "deleted", "changed": not item["deleted"]}
                        )
            public["status"] = "running"
            try:
                if public["destination"] == "gmail":
                    from .gmail_actions import trash_threads

                    response = await trash_threads(self.service.gmail, plan["account"], items)
                    results = response["results"]
                    if (
                        response.get("account") != plan["account"]
                        or len(results) != len(items)
                        or {result["id"] for result in results} != set(plan["ids"])
                        or any(
                            result["status"] not in SUCCESS | {"failed", "unverified", "skipped"}
                            for result in results
                        )
                    ):
                        raise ValueError("Invalid Gmail operation receipt")
                    with self.service.db.connection() as conn:
                        for result in results:
                            if result["status"] in {"trashed", "already_trashed"}:
                                MailStore.patch_local(conn, result["id"], {"deleted": True})
                public["result"] = {
                    "changed": sum(
                        item.get("changed", item["status"] in {"deleted", "trashed"})
                        for item in results
                    ),
                    "failed": sum(item["status"] == "failed" for item in results),
                    "unverified": sum(item["status"] == "unverified" for item in results),
                    "skipped": sum(item["status"] == "skipped" for item in results),
                    "already_trashed": sum(item["status"] == "already_trashed" for item in results),
                    "results": results,
                }
                succeeded = sum(item["status"] in SUCCESS for item in results)
                public["status"] = (
                    "done" if succeeded == len(items) else "partial" if succeeded else "failed"
                )
            except (Exception, asyncio.CancelledError) as exc:
                public["status"] = "failed"
                public["error"] = (
                    "Operation interrupted; Gmail state must be checked before retrying"
                )
                public["result"] = {
                    "changed": 0,
                    "failed": 0,
                    "unverified": len(items),
                    "skipped": 0,
                    "results": [{"id": item["id"], "status": "unverified"} for item in items],
                }
                if isinstance(exc, asyncio.CancelledError):
                    self.persist_receipt(plan)
                    raise
            self.persist_receipt(plan)
            return public


class MailDeletionTools:
    def __init__(self, service, inbox, conversation_id):
        self.service, self.inbox, self.conversation_id = service, inbox, conversation_id

    def definitions(self, define):
        return [
            define(
                "preview_mail_deletion",
                "Prepare a deletion plan for user confirmation in Chat. Does not delete. "
                "Choose local Inbox or Gmail Trash; ask if the destination is unclear. "
                "Supply either mail_ids from search_inbox or search filters. Dates use YYYY-MM-DD; "
                "before is exclusive midnight in the user's timezone. Gmail moves whole threads "
                "to Trash, up to 20 per confirmation; local plans contain up to 2000 cached mails. "
                "Report remaining matches and unknown dates. Only the user can execute this plan.",
                {
                    "destination": {"type": "string", "enum": ["local", "gmail"]},
                    "mail_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 2000},
                    **{
                        key: {"type": "string", "maxLength": 2000}
                        for key in ("query", "sender", "subject", "before", "after")
                    },
                    "include_deleted": {"type": "boolean"},
                    "include_ignored": {"type": "boolean"},
                },
                ["destination"],
            )
        ]

    async def run(self, args):
        from .chat import local_io

        return {
            "mail_deletion_plan": await local_io(
                self.service.mail_deletions.create, self.inbox, self.conversation_id, args
            ),
            "requires_user_confirmation": True,
        }


class ConfirmDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


def build_deletion_router(service):
    router = APIRouter(prefix="/inbox-plans")

    @router.get("/{plan_id}")
    def get(plan_id: str):
        return service.mail_deletions.get(plan_id)["public"]

    @router.post("/{plan_id}/apply")
    async def apply(plan_id: str, value: ConfirmDeletion):
        return await service.mail_deletions.apply(plan_id, value.expected_version)

    return router
