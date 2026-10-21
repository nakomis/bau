from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import bau
import bau.version
from bau.abandon import (
    RUN_IN_PROGRESS,
    AlreadyAbandoned,
    InvalidRef,
    RunInProgress,
    UnknownProject,
    abandon,
)
from bau.answers import MissingAnswer, NotWaiting, StaleRound, mark_not_bau, submit_answers
from bau.checkout import Repos
from bau.config import Config, ProjectConfig
from bau.github import GitHubClient
from bau.markers import Answer
from bau.plane import PlaneClient
from bau.snapshot import drop_open_pr, drop_question, read_snapshot

log = logging.getLogger(__name__)

# The dashboard is a same-origin static page that fetches /state.json and posts
# to /abandon, so nothing outside 'self' is needed (fonts are bundled).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# The page names its hashed assets, so it must be re-checked on every load
# after an upgrade; the hashed assets themselves may be cached.
NO_CACHE_PATHS = {"/", "/index.html"}


def static_dir() -> Path | None:
    """The dashboard to serve: the copy packaged in the wheel, else the checkout's build, else none."""
    root = Path(bau.__file__).parent
    for candidate in (root / "web_dist", root.parent / "web" / "dist"):
        if candidate.is_dir():
            return candidate
    return None


class Forbidden(Exception):
    pass


class AbandonBody(BaseModel):
    note: str = Field(default="", max_length=500)


class AnswerBody(BaseModel):
    id: str = Field(max_length=100)
    accepted: bool
    text: str = Field(default="", max_length=2000)


class AnswersBody(BaseModel):
    round: int
    asked_at: str = Field(max_length=100)
    answers: list[AnswerBody] = Field(max_length=20)


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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path in NO_CACHE_PATHS:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/state.json")
    def state():
        snap = read_snapshot(cfg.paths.snapshot)
        body = dict(snap) if snap is not None else {"schema": 1, "missing": True}
        body["version"] = {"version": bau.version.VERSION, "sha": bau.version.GIT_SHA}
        return JSONResponse(
            body,
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
                lock_path=cfg.paths.lock,
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
        try:
            drop_open_pr(cfg.paths.snapshot, r.ref)
        except Exception as e:  # noqa: BLE001 - the abandon itself succeeded; the next snapshot corrects this
            log.warning("snapshot update after abandon %s failed: %s", ref, type(e).__name__)
        return {"ref": r.ref, "pr": r.pr, "restored_state": r.restored_state}

    lock_path = cfg.paths.lock

    def question_write(ref: str, what: str, action: Callable[[], None]):
        try:
            action()
        except InvalidRef:
            return JSONResponse({"error": "invalid ref"}, status_code=400)
        except UnknownProject:
            return JSONResponse({"error": "unknown project"}, status_code=400)
        except MissingAnswer as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RunInProgress:
            return JSONResponse({"error": RUN_IN_PROGRESS}, status_code=409)
        except (StaleRound, NotWaiting) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except Exception as e:  # noqa: BLE001 - never echo text that could contain tokens
            log.error("%s %s failed: %s", what, ref, type(e).__name__)
            return JSONResponse({"error": "internal error"}, status_code=500)
        try:
            drop_question(cfg.paths.snapshot, ref.strip())
        except Exception as e:  # noqa: BLE001 - the write itself succeeded; the next snapshot corrects this
            log.warning("snapshot update after %s %s failed: %s", what, ref, type(e).__name__)
        return {"ref": ref.strip()}

    @app.post("/answers/{ref}", dependencies=[Depends(require_token)])
    def answers_route(ref: str, body: AnswersBody):
        answers = [Answer(a.id, a.accepted, a.text) for a in body.answers]
        return question_write(
            ref,
            "answers",
            lambda: submit_answers(
                ref,
                body.round,
                body.asked_at,
                answers,
                plane=plane,
                projects=projects,
                worker_email=cfg.identities.worker_plane_email,
                lock_path=lock_path,
            ),
        )

    @app.post("/not-bau/{ref}", dependencies=[Depends(require_token)])
    def not_bau_route(ref: str, body: AbandonBody | None = None):
        return question_write(
            ref,
            "not-bau",
            lambda: mark_not_bau(
                ref, body.note if body else "", plane=plane, projects=projects, lock_path=lock_path
            ),
        )

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def serve(app: FastAPI, port: int = 8800) -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 - Leia proxies to Cere's LAN address
