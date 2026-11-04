"""gitadata HTTP API.

auth is per visitor, not per server: each user signs in with their own github
account through /auth/github/start, and every request after that carries a
signed http-only cookie pointing at a token held in this process. no token is
written to disk, exposed to browser javascript, or shared between users.

for local development only, GITADATA_DEV_PAT=1 borrows $GITHUB_TOKEN so the ui
can be exercised before an oauth app exists. /api/me reports that as dev.
"""

from __future__ import annotations

import os
import pathlib
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import github as G
from . import oauth as O
from . import sessions as S

WEB_DIR = pathlib.Path(__file__).parent / "web"
COOKIE = "gitadata_session"

app = FastAPI(title="gitadata", version="0.2.0", description="commit metadata editor")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

AUTH = O.OAuth(O.config_from_env())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@app.get("/auth/github/start", include_in_schema=False)
def auth_start() -> RedirectResponse:
    try:
        return RedirectResponse(AUTH.begin(), status_code=302)
    except O.OAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/auth/github/callback", include_in_schema=False)
def auth_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?auth=denied", status_code=302)
    try:
        auth = AUTH.complete(code, state)
    except O.OAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    response = RedirectResponse("/", status_code=302)
    assert AUTH.config
    response.set_cookie(
        COOKIE,
        AUTH.cookie_value(auth),
        max_age=O.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=AUTH.config.secure_cookie,
        path="/",
    )
    return response


@app.post("/auth/logout", include_in_schema=False)
def auth_logout(request: Request) -> Response:
    AUTH.logout(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


def current_user(request: Request) -> O.AuthUser:
    user = AUTH.user_from_cookie(request.cookies.get(COOKIE)) or AUTH.dev_user()
    if user is None:
        raise HTTPException(status_code=401, detail="sign in with github")
    return user


CurrentUser = Annotated[O.AuthUser, Depends(current_user)]


@app.get("/api/me")
def me(user: CurrentUser) -> dict[str, Any]:
    return user.public()


@app.get("/api/auth/config", include_in_schema=False)
def auth_config() -> dict[str, Any]:
    return {"oauth": AUTH.enabled, "dev": AUTH.dev_enabled()}


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

@app.exception_handler(S.SessionError)
def _session_error(_, exc: S.SessionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(G.GitHubError)
def _github_error(_, exc: G.GitHubError) -> JSONResponse:
    status = exc.status if 400 <= exc.status < 600 else 502
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.exception_handler(O.OAuthError)
def _oauth_error(_, exc: O.OAuthError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# repository and session surface
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "oauth": AUTH.enabled,
        "dev": AUTH.dev_enabled(),
        "sessions": len(S.sessions_snapshot()),
    }


@app.get("/api/github/me")
def github_me(user: CurrentUser) -> dict[str, Any]:
    return {"login": user.login, "name": user.name, "avatar": user.avatar}


@app.get("/api/github/repos")
def repos(user: CurrentUser, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return {"repos": G.list_repos(user.token, limit=limit)}


@app.post("/api/sessions")
def open_session(user: CurrentUser, payload: dict = Body(...)) -> dict[str, Any]:
    owner = (payload.get("owner") or "").strip()
    repo = (payload.get("repo") or "").strip()
    if "/" in repo and not owner:
        owner, repo = repo.split("/", 1)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="owner and repo are required")
    session = S.create(owner, repo, user.token, user.user_id)
    return {
        "session": session.id,
        "repo": session.full_name,
        "default_branch": session.default_branch,
        "empty": session.empty,
        "remote_refs": session.base_refs,
    }


@app.get("/api/sessions")
def list_sessions(user: CurrentUser) -> dict[str, Any]:
    return {"sessions": S.sessions_snapshot(user.user_id)}


@app.get("/api/sessions/{sid}/graph")
def graph(sid: str, user: CurrentUser, ref: str | None = None) -> dict[str, Any]:
    """`ref` narrows the walk to one branch, e.g. ?ref=main or ?ref=refs/heads/main."""
    return S.graph(sid, user.user_id, ref)


@app.post("/api/sessions/{sid}/plan")
def plan(sid: str, user: CurrentUser, payload: dict = Body(...)) -> dict[str, Any]:
    return S.plan_edits(sid, payload.get("edits") or {}, payload.get("refs"), user_id=user.user_id)


@app.post("/api/sessions/{sid}/apply")
def apply(sid: str, user: CurrentUser, payload: dict = Body(...)) -> dict[str, Any]:
    return S.apply_edits(
        sid,
        payload.get("edits") or {},
        payload.get("refs"),
        token=user.token,
        push=bool(payload.get("push")),
        include_tags=payload.get("include_tags", True),
        user_id=user.user_id,
    )


@app.get("/api/sessions/{sid}/backups")
def backups(sid: str, user: CurrentUser) -> dict[str, Any]:
    return S.backups(sid, user.user_id)


@app.post("/api/sessions/{sid}/undo")
def undo(sid: str, user: CurrentUser, payload: dict = Body(...)) -> dict[str, Any]:
    ts = (payload.get("ts") or "").strip()
    if not ts:
        raise HTTPException(status_code=400, detail="ts is required")
    return S.undo(sid, ts, user.user_id)


@app.delete("/api/sessions/{sid}")
def close_session(sid: str, user: CurrentUser) -> dict[str, Any]:
    S.get(sid, user.user_id)  # ownership check before deleting anything
    return {"closed": S.destroy(sid)}


@app.post("/api/sessions/sweep")
def sweep(user: CurrentUser, ttl: int | None = None) -> dict[str, Any]:
    return {"closed": S.sweep(ttl or S.SESSION_TTL), "orphans": S.sweep_orphans(grace_s=0)}


@app.on_event("startup")
def _startup() -> None:
    S.sweep_orphans()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
