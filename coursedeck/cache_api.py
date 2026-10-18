"""REST bindings for the same bounded, offline queries exposed by MCP."""

import sqlite3
from functools import wraps

from fastapi import APIRouter, HTTPException

from .cache_query import (
    CACHE_UNAVAILABLE_MESSAGE,
    INVALID_QUERY_MESSAGE,
    CacheQuery,
    CacheQueryError,
)

# An explicit allowlist, never method lookup from caller-controlled input.
QUERY_TOOLS = {
    "search_tasks": "tasks",
    "get_task": "task",
    "search_courses": "courses",
    "search_grades": "grades",
    "search_documents": "documents",
    "read_document": "document",
    "search_mail": "mail",
    "read_mail": "mail-message",
    "list_changes": "changes",
    "health": "health",
}


def cache_endpoint(method):
    @wraps(method)
    def read(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except CacheQueryError as exc:
            raise HTTPException(
                exc.status_code, {"category": exc.category, "message": str(exc)}
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(
                503, {"category": "cache_unavailable", "message": CACHE_UNAVAILABLE_MESSAGE}
            ) from exc
        except ValueError as exc:
            # Inherited query/validation errors can contain cached text or caller input.
            raise HTTPException(
                400, {"category": "invalid_query", "message": INVALID_QUERY_MESSAGE}
            ) from exc

    return read


def build_cache_router(query: CacheQuery):
    router = APIRouter(prefix="/api/cache", tags=["cached queries"])
    for method, path in QUERY_TOOLS.items():
        router.add_api_route(
            "/" + path,
            cache_endpoint(getattr(query, method)),
            methods=["GET"],
            responses={
                400: {"description": "Invalid cached query"},
                404: {"description": "Record not found in cache"},
                503: {"description": "Cache unavailable or recovery in progress"},
            },
        )
    return router
