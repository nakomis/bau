from __future__ import annotations

import hmac
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bau.abandon import RUN_IN_PROGRESS, AlreadyAbandoned, RunInProgress, abandon
from bau.checkout import Repos
from bau.config import Config, ProjectConfig
from bau.github import GitHubClient
from bau.plane import PlaneClient
from bau.snapshot import read_snapshot


class AbandonBody(BaseModel):
    note: str = ""


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

    @app.post("/abandon/{ref}")
    def abandon_route(
        ref: str,
        body: AbandonBody | None = None,
        x_bau_proxy_token: str | None = Header(default=None),
    ):
        supplied = (x_bau_proxy_token or "").encode()
        if not proxy_token or not hmac.compare_digest(supplied, proxy_token.encode()):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            r = abandon(
                ref,
                plane=plane,
                github_factory=github_factory,
                repos=repos,
                projects=projects,
                worker_login=cfg.identities.worker_github_login,
                note=body.note if body else "",
                lock_path=cfg.paths.logs.parent / "run.lock",
            )
        except RunInProgress:
            return JSONResponse({"error": RUN_IN_PROGRESS}, status_code=409)
        except AlreadyAbandoned as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"ref": r.ref, "pr": r.pr, "restored_state": r.restored_state}

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def serve(app: FastAPI, port: int = 8800) -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 - Leia proxies to Cere's LAN address
