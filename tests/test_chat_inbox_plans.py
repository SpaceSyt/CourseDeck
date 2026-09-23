import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_chat import configured, reply
from test_chat_tools import call

from coursedeck.chat import ChatMessageInput
from coursedeck.chat_inbox import InboxTools
from coursedeck.chat_inbox_plans import build_deletion_router
from coursedeck.mail import MailMessage, MailStore


@pytest.fixture
async def case(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    service.db.save_settings(
        service.db.settings().model_copy(update={"timezone": "America/New_York"})
    )
    store = MailStore(service.db)
    for key, stamp in [
        ("old", "2026-07-30T23:00:00-04:00"),
        ("boundary", "2026-07-31T00:00:00-04:00"),
        ("unknown", None),
    ]:
        store.upsert(
            MailMessage(
                id="gmail:" + key,
                subject="Course notes " + key,
                sender="Course staff",
                sender_email="staff@example.edu",
                received_at=stamp,
                body="Lecture",
                body_complete=True,
            )
        )
    cid = service.store.create_conversation("Mail", None)
    inbox = InboxTools(service, ChatMessageInput(message="2026.7.31"), lambda docs, **_: docs)
    return SimpleNamespace(
        service=service, store=store, inbox=inbox, cid=cid, plans=service.mail_deletions
    )


async def test_date_plan_only_changes_exact_reviewed_ids_after_confirmation(case):
    plan = case.plans.create(case.inbox, case.cid, {"destination": "local", "before": "2026-07-31"})
    assert plan["matched"] == 1 and plan["missing_date_count"] == 1
    assert case.inbox.seen == set()
    assert not case.store.get("gmail:old")["deleted"]
    case.store.upsert(
        MailMessage(
            id="gmail:arrived",
            sender="Course staff",
            subject="Course notes arrived",
            received_at="2026-07-20T00:00:00Z",
        )
    )
    result = await case.plans.apply(plan["id"], plan["version"])
    assert result["status"] == "done" and result["result"]["changed"] == 1
    assert case.store.get("gmail:old")["deleted"]
    for key in ("boundary", "unknown", "arrived"):
        assert not case.store.get("gmail:" + key)["deleted"]
    assert await case.plans.apply(plan["id"], plan["version"]) == result


async def test_changed_mail_invalidates_preview_without_deleting(case):
    plan = case.plans.create(case.inbox, case.cid, {"destination": "local", "before": "2026-07-31"})
    case.store.patch("gmail:old", {"read": True})
    with pytest.raises(HTTPException) as failure:
        await case.plans.apply(plan["id"], plan["version"])
    assert failure.value.status_code == 409
    assert not case.store.get("gmail:old")["deleted"]


async def test_ids_require_same_turn_discovery_and_saved_activity_keeps_receipt(case):
    args = {"destination": "local", "mail_ids": ["gmail:old"]}
    with pytest.raises(ValueError, match="search_inbox"):
        case.plans.create(case.inbox, case.cid, args)
    case.inbox.search({"query": "old"})
    plan = case.plans.create(case.inbox, case.cid, args)
    message = case.service.store.add_message(
        case.cid,
        "assistant",
        "Confirm the preview",
        activity=[{"type": "activity", "mail_deletion_plan": plan}],
    )
    await case.plans.apply(plan["id"], plan["version"])
    saved = case.service.store.conversation(case.cid)["messages"]
    receipt = next(row for row in saved if row["id"] == message["id"])["activity"][0]
    assert receipt["mail_deletion_plan"]["status"] == "done"


async def test_apply_endpoint_requires_version_and_does_not_need_model(case):
    plan = case.plans.create(case.inbox, case.cid, {"destination": "local", "before": "2026-07-31"})
    app = FastAPI()
    app.include_router(build_deletion_router(case.service))
    with TestClient(app) as client:
        assert client.get("/inbox-plans/" + plan["id"]).json()["status"] == "pending"
        assert client.post("/inbox-plans/" + plan["id"] + "/apply", json={}).status_code == 422
        result = client.post(
            "/inbox-plans/" + plan["id"] + "/apply", json={"expected_version": plan["version"]}
        )
        assert result.status_code == 200 and result.json()["status"] == "done"


async def test_model_can_propose_date_deletion_but_cannot_execute_it(case):
    import httpx

    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return reply(
                None, [call("preview_mail_deletion", destination="local", before="2026-07-31")]
            )
        output = json.loads(payload["messages"][-1]["content"])
        assert output["requires_user_confirmation"]
        return reply("请确认删除清单。")

    case.service.transport = httpx.MockTransport(handler)
    result = await case.service.send(ChatMessageInput(message="本地删除2026.7.31之前的邮件"))
    assert not case.store.get("gmail:old")["deleted"]
    receipt = next(
        event["mail_deletion_plan"]
        for event in result["message"]["activity"]
        if "mail_deletion_plan" in event
    )
    assert receipt["matched"] == 1


async def test_gmail_partial_result_only_hides_confirmed_threads(case, monkeypatch):
    case.service.gmail = SimpleNamespace(status=lambda: {"status": "connected"})
    case.service.db.update_state("gmail", account="owner@example.edu", authorized=True)
    plan = case.plans.create(case.inbox, case.cid, {"destination": "gmail"})
    assert plan["missing_date_count"] == 0

    async def trash(gmail, account, items):
        return {
            "account": account,
            "results": [
                {
                    "id": item["id"],
                    "status": "trashed" if item["id"] == "gmail:old" else "unverified",
                }
                for item in items
            ],
        }

    monkeypatch.setattr("coursedeck.gmail_actions.trash_threads", trash)
    result = await case.plans.apply(plan["id"], plan["version"])
    assert result["status"] == "partial"
    assert result["result"]["changed"] == 1 and result["result"]["unverified"] == 2
    assert case.store.get("gmail:old")["deleted"]
    assert not case.store.get("gmail:boundary")["deleted"]
    assert not case.store.get("gmail:unknown")["deleted"]
