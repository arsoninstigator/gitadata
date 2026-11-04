"""gitadata HTTP API.

    uvicorn gitadata.server:app --reload --port 8000

Auth model for this stage: the server holds one GitHub token in its own
environment and acts only on repositories that token can push to. That is a
single-tenant dev deployment; the multi-tenant shape is a GitHub App
installation token per request, which changes only `_token()` and nothing else.
"""

from __future__ import annotations

import os
from typing import Any

import pathlib

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import github as G
from . import sessions as S

WEB_DIR = pathlib.Path(__file__).parent / "web"

app = FastAPI(title="gitadata", version="0.1.0", description="commit metadata editor")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


def _token() -> str:
    return G.token_from_env()


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, S.SessionError):
        return HTTPException(status_code=exc.status, detail=str(exc))
    if isinstance(exc, G.GitHubError):
        status = exc.status if 400 <= exc.status < 600 else 502
        return HTTPException(status_code=status, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@app.exception_handler(S.SessionError)
def _session_error(_, exc: S.SessionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(G.GitHubError)
def _github_error(_, exc: G.GitHubError) -> JSONResponse:
    status = exc.status if 400 <= exc.status < 600 else 502
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.on_event("startup")
def _startup() -> None:
    S.sweep_orphans()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "sessions": len(S.sessions_snapshot())}


@app.get("/api/github/me")
def me() -> dict[str, Any]:
    try:
        user = G.whoami(_token())
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)
    return {"login": user.get("login"), "name": user.get("name")}


@app.get("/api/github/repos")
def repos(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    try:
        return {"repos": G.list_repos(_token(), limit=limit)}
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.post("/api/sessions")
def open_session(payload: dict = Body(...)) -> dict[str, Any]:
    owner = (payload.get("owner") or "").strip()
    repo = (payload.get("repo") or "").strip()
    if "/" in repo and not owner:
        owner, repo = repo.split("/", 1)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="owner and repo are required")
    try:
        session = S.create(owner, repo, _token())
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)
    return {
        "session": session.id,
        "repo": session.full_name,
        "default_branch": session.default_branch,
        "empty": session.empty,
        "remote_refs": session.base_refs,
    }


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    return {"sessions": S.sessions_snapshot()}


@app.get("/api/sessions/{sid}/graph")
def graph(sid: str) -> dict[str, Any]:
    try:
        return S.graph(sid)
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.post("/api/sessions/{sid}/plan")
def plan(sid: str, payload: dict = Body(...)) -> dict[str, Any]:
    try:
        return S.plan_edits(sid, payload.get("edits") or {}, payload.get("refs"))
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.post("/api/sessions/{sid}/apply")
def apply(sid: str, payload: dict = Body(...)) -> dict[str, Any]:
    try:
        return S.apply_edits(
            sid,
            payload.get("edits") or {},
            payload.get("refs"),
            token=_token(),
            push=bool(payload.get("push")),
            include_tags=payload.get("include_tags", True),
        )
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.get("/api/sessions/{sid}/backups")
def backups(sid: str) -> dict[str, Any]:
    try:
        return S.backups(sid)
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.post("/api/sessions/{sid}/undo")
def undo(sid: str, payload: dict = Body(...)) -> dict[str, Any]:
    ts = (payload.get("ts") or "").strip()
    if not ts:
        raise HTTPException(status_code=400, detail="ts is required")
    try:
        return S.undo(sid, ts)
    except (G.GitHubError, S.SessionError) as exc:
        raise _fail(exc)


@app.delete("/api/sessions/{sid}")
def close_session(sid: str) -> dict[str, Any]:
    return {"closed": S.destroy(sid)}


@app.post("/api/sessions/sweep")
def sweep(ttl: int | None = None) -> dict[str, Any]:
    return {"closed": S.sweep(ttl or S.SESSION_TTL), "orphans": S.sweep_orphans(grace_s=0)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
