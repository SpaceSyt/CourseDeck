"""Supported reading strategies, shared by automatic and on-demand collection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadingCapabilities:
    assignments: bool = True
    materials: str | None = None
    announcements: bool = False


CAPABILITIES = {
    "brightspace": ReadingCapabilities(materials="brightspace_content", announcements=True),
    "google_classroom": ReadingCapabilities(materials="classroom_pages", announcements=True),
    "gradescope": ReadingCapabilities(),
    "webassign": ReadingCapabilities(),
    "rephactor": ReadingCapabilities(),
}


def reading_capabilities(provider):
    return CAPABILITIES.get(provider, ReadingCapabilities())


def connector_capabilities(connector, provider):
    """Use the connector's declaration; built-in defaults also support older test doubles."""
    declared = getattr(connector, "reading_capabilities", None)
    if isinstance(declared, ReadingCapabilities):
        return declared
    active = getattr(connector, "active", None)
    if active is not None and active is not connector:
        return connector_capabilities(active, provider)
    return reading_capabilities(provider)
