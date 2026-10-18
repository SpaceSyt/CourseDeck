from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_materials import setup

from coursedeck.connectors.base import Connector
from coursedeck.connectors.capabilities import ReadingCapabilities, connector_capabilities
from coursedeck.domain import Outcome, SyncResult
from coursedeck.library import material_sync


async def test_connector_disabling_materials_skips_automatic_dispatch(tmp_path):
    _, course, engine, collector = setup(tmp_path, None)
    engine.connectors["brightspace"].reading_capabilities = ReadingCapabilities(materials=None)
    collector.refresh_source = AsyncMock()
    response = await material_sync(collector.db, collector, collector.knowledge)(
        "brightspace", SyncResult(outcome=Outcome.SUCCESS, courses=[course])
    )
    assert response == {}
    collector.refresh_source.assert_not_awaited()
    result = {"documents": [], "warnings": []}
    await collector.collect("brightspace", [course], "notes", result)
    assert not result["documents"]
    assert "not supported" in result["warnings"][0]


async def test_unknown_declared_reader_reports_error_without_opening_browser(tmp_path):
    _, course, engine, collector = setup(tmp_path, None)
    engine.connectors["brightspace"].reading_capabilities = ReadingCapabilities(
        materials="future_reader"
    )
    result = await collector.refresh_source("brightspace", courses=[course])
    assert not result["documents"]
    assert "not supported" in result["warnings"][0]
    assert result["errors"] == [
        {
            "category": "unsupported_strategy",
            "scope": "brightspace",
            "stage": "materials_read",
            "retry_attempt": 0,
        }
    ]


@pytest.mark.parametrize("facade_declares", [True, False])
def test_facade_declared_capability_overrides_active_or_inherits_it(facade_declares):
    active = SimpleNamespace(reading_capabilities=ReadingCapabilities(materials=None))
    facade = SimpleNamespace(active=active)
    if facade_declares:
        facade.reading_capabilities = ReadingCapabilities(materials="classroom_pages")
    capabilities = connector_capabilities(facade, "google_classroom")
    assert capabilities.materials == ("classroom_pages" if facade_declares else None)
    assert (
        connector_capabilities(SimpleNamespace(), "brightspace").materials == "brightspace_content"
    )


def test_inherited_base_capability_delegates_to_active_connector():
    facade = SimpleNamespace(
        key="google_classroom",
        active=SimpleNamespace(reading_capabilities=ReadingCapabilities(materials=None)),
    )
    assert Connector.reading_capabilities.fget(facade).materials is None
