from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bau.abandon import (
    RUN_IN_PROGRESS,
    AlreadyAbandoned,
    InvalidRef,
    RunInProgress,
    UnknownProject,
    abandon,
)
from bau.checkout import Repos
from bau.config import Config, ProjectConfig
from bau.github import GitHubClient
from bau.plane import PlaneClient
from bau.snapshot import read_snapshot

log = logging.getLogger(__name__)


class Forbidden(Exception):
    pass


class AbandonBody(BaseModel):
    note: str = Field(default="", max_length=500)


def create_app(
    cfg: Config,
    projects: dict[str, ProjectConfig],
    plane: PlaneClient,
    github_factory: Callable[[str], GitHubClient],
    repos: Repos,
    static_dir: Path | None,
    proxy_token: str,
) -> FastAPI:
    app = FastAPI(title="bau")

    @app.get("/state.json")
    def state():
        snap = read_snapshot(cfg.paths.snapshot)
        return JSONResponse(
            snap if snap is not None else {"schema": 1, "missing": True},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Forbidden)
    def forbidden(request: Request, exc: Forbidden):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    def require_token(x_bau_proxy_token: str | None = Header(default=None)) -> None:
        supplied = (x_bau_proxy_token or "").encode()
        if not proxy_token or not hmac.compare_digest(supplied, proxy_token.encode()):
            raise Forbidden

    @app.post("/abandon/{ref}", dependencies=[Depends(require_token)])
    def abandon_route(ref: str, body: AbandonBody | None = None):
        try:
            r = abandon(
                ref,
                plane=plane,
                github_factory=github_factory,
                repos=repos,
                projects=projects,
                worker_login=cfg.identities.worker_github_login,
                worker_email=cfg.identities.worker_plane_email,
                note=body.note if body else "",
                lock_path=cfg.paths.logs.parent / "run.lock",
            )
        except InvalidRef:
            return JSONResponse({"error": "invalid ref"}, status_code=400)
        except UnknownProject:
            return JSONResponse({"error": "unknown project"}, status_code=400)
        except RunInProgress:
            return JSONResponse({"error": RUN_IN_PROGRESS}, status_code=409)
        except AlreadyAbandoned as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except Exception as e:  # noqa: BLE001 - never echo text that could contain tokens
            log.error("abandon %s failed: %s", ref, type(e).__name__)
            return JSONResponse({"error": "internal error"}, status_code=500)
        return {"ref": r.ref, "pr": r.pr, "restored_state": r.restored_state}

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def serve(app: FastAPI, port: int = 8800) -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 - Leia proxies to Cere's LAN address
