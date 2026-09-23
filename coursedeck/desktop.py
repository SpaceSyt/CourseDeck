"""Local desktop preferences; operating-system state is not part of data backups."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from .autostart import Autostart


class StartupPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def build_desktop_router(directory):
    router = APIRouter(prefix="/api/desktop")

    def manager(request):
        server = request.scope.get("server")
        return Autostart(directory, server[1] if server else 48321)

    @router.get("/autostart")
    def startup_status(request: Request):
        try:
            return manager(request).status()
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "Could not read the Windows startup setting.") from exc

    @router.put("/autostart")
    def set_startup(value: StartupPreference, request: Request):
        startup = manager(request)
        try:
            if not startup.status()["supported"]:
                raise HTTPException(409, "Automatic startup is available on Windows only.")
            return startup.set_enabled(value.enabled)
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "Could not change the Windows startup setting.") from exc

    return router
