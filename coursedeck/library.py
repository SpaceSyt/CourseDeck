"""Automatic evidence collection runs without a model, under the source sync lock."""

from .connectors.capabilities import connector_capabilities
from .domain import now


def material_sync(db, collector, store):
    async def refresh(provider, result):
        connectors = getattr(getattr(collector, "engine", None), "connectors", {})
        if connector_capabilities(connectors.get(provider), provider).materials is None:
            return {}
        excluded = {
            source_id
            for course in db.courses()
            if course.get("deleted") or course.get("disabled")
            for source_id in course.get("source_course_ids", [course["id"]])
        }
        courses = [course for course in result.courses if course.id not in excluded]
        if not courses:
            return {"metadata": {"materials": {"status": "skipped", "count": 0}}}
        collected = await collector.refresh_source(
            provider,
            course_ids=[course.external_id for course in courses],
            already_locked=True,
            courses=courses,
        )
        documents = collected.get("documents", [])
        store.upsert_documents(documents)
        documents_by_id = {document["id"]: document for document in documents}
        for task in result.tasks:
            document = documents_by_id.get(task.id)
            if document and document.get("complete") is True and document.get("body"):
                task.description = document["body"]
                task.raw_data["unavailable_fields"] = [
                    field
                    for field in task.raw_data.get("unavailable_fields", [])
                    if field != "description"
                ]
        warnings = collected.get("warnings", [])
        return {
            "warnings": [f"Materials: {warning}" for warning in warnings],
            "metadata": {
                "materials": {
                    "status": "partial" if warnings else "success",
                    "count": len(documents),
                    "checked_at": now().isoformat(),
                    "warnings": warnings,
                    "errors": collected.get("errors", []),
                }
            },
        }

    return refresh
