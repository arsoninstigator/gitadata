"""github oauth web flow (authorization code with pkce).

this is the product's auth path: each visitor authorizes their own github
account, and the token that results lives only in this process, only for that
visitor, addressed by a signed cookie. there is no server-wide token.

the one exception is local development, where GITADATA_DEV_PAT=1 lets the
process borrow $GITHUB_TOKEN so the ui can be worked on without registering an
oauth app. it is off by default and reports itself as dev in /api/me.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import github as G

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
PENDING_TTL = 600
SESSION_TTL = 8 * 3600


class OAuthError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    cookie_secret: str
    base_url: str
    scopes: tuple[str, ...] = ("repo",)

    @property
    def callback_url(self) -> str:
        return self.base_url.rstrip("/") + "/auth/github/callback"

    @property
    def secure_cookie(self) -> bool:
        return self.base_url.startswith("https://")


def config_from_env() -> Config | None:
    client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return None
    return Config(
        client_id=client_id,
        client_secret=client_secret,
        cookie_secret=os.environ.get("GITADATA_SECRET", "").strip() or secrets.token_hex(32),
        base_url=os.environ.get("GITADATA_BASE_URL", "http://localhost:8000").strip(),
        scopes=tuple(
            s.strip() for s in os.environ.get("GITHUB_OAUTH_SCOPES", "repo").split(",") if s.strip()
        ),
    )


@dataclasses.dataclass
class AuthUser:
    sid: str
    token: str
    login: str
    user_id: int
    name: str = ""
    avatar: str = ""
    scopes: tuple[str, ...] = ()
    issued: float = dataclasses.field(default_factory=time.time)
    dev: bool = False

    @property
    def expired(self) -> bool:
        return time.time() - self.issued > SESSION_TTL

    def public(self) -> dict:
        return {
            "login": self.login,
            "id": self.user_id,
            "name": self.name,
            "avatar": self.avatar,
            "dev": self.dev,
            "scopes": list(self.scopes),
        }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class OAuth:
    """In-memory identity store. Restart means everyone signs in again."""

    def __init__(self, config: Config | None):
        self.config = config
        self._pending: dict[str, tuple[str, float]] = {}
        self._users: dict[str, AuthUser] = {}
        self._dev: AuthUser | None = None

    # ------------------------------------------------------------------ setup

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def dev_enabled(self) -> bool:
        return (os.environ.get("GITADATA_DEV_PAT", "").strip() == "1") and bool(
            os.environ.get("GITHUB_TOKEN", "").strip()
        )

    # ------------------------------------------------------------------ flow

    def begin(self) -> str:
        if not self.enabled:
            raise OAuthError(
                "github oauth is not configured: set GITHUB_OAUTH_CLIENT_ID and "
                "GITHUB_OAUTH_CLIENT_SECRET"
            )
        assert self.config
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        self._pending[state] = (verifier, time.time())
        self._prune()
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.callback_url,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "allow_signup": "true",
        }
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    def complete(self, code: str, state: str) -> AuthUser:
        if not self.enabled:
            raise OAuthError("github oauth is not configured")
        assert self.config
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthError("unknown or reused oauth state")
        verifier, started = pending
        if time.time() - started > PENDING_TTL:
            raise OAuthError("oauth state expired, start again")

        payload = self._exchange(code, verifier)
        token = payload.get("access_token")
        if not token:
            raise OAuthError(payload.get("error_description") or payload.get("error") or "no token")
        scopes = tuple(s for s in (payload.get("scope") or "").split(",") if s)

        user = G.whoami(token)
        auth = AuthUser(
            sid=secrets.token_urlsafe(24),
            token=token,
            login=user.get("login", ""),
            user_id=int(user.get("id") or 0),
            name=user.get("name") or "",
            avatar=user.get("avatar_url") or "",
            scopes=scopes,
        )
        self._users[auth.sid] = auth
        return auth

    def _exchange(self, code: str, verifier: str) -> dict:
        assert self.config
        body = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": code,
                "redirect_uri": self.config.callback_url,
                "code_verifier": verifier,
            }
        ).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", G.UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise OAuthError(f"token exchange failed: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OAuthError(f"token exchange unreachable: {exc.reason}") from exc

    # ------------------------------------------------------------------ store

    def _prune(self) -> None:
        now = time.time()
        for state, (_, started) in list(self._pending.items()):
            if now - started > PENDING_TTL:
                self._pending.pop(state, None)
        for sid, user in list(self._users.items()):
            if user.expired:
                self._users.pop(sid, None)

    def user_from_cookie(self, value: str | None) -> AuthUser | None:
        if not value or not self.config:
            return None
        sid = self._verify(value)
        if not sid:
            return None
        user = self._users.get(sid)
        if user is None or user.expired:
            return None
        return user

    def dev_user(self) -> AuthUser | None:
        if not self.dev_enabled():
            return None
        if self._dev is None:
            token = G.token_from_env()
            user = G.whoami(token)
            self._dev = AuthUser(
                sid="dev",
                token=token,
                login=user.get("login", ""),
                user_id=int(user.get("id") or 0),
                name=user.get("name") or "",
                avatar=user.get("avatar_url") or "",
                dev=True,
            )
        return self._dev

    def logout(self, value: str | None) -> None:
        if not value:
            return
        sid = self._verify(value)
        if sid:
            self._users.pop(sid, None)

    # ------------------------------------------------------------------ cookie

    def cookie_value(self, auth: AuthUser) -> str:
        return f"{auth.sid}.{self._sign(auth.sid)}"

    def _sign(self, sid: str) -> str:
        secret = (self.config.cookie_secret if self.config else "").encode()
        return hmac.new(secret, sid.encode(), hashlib.sha256).hexdigest()[:32]

    def _verify(self, value: str) -> str | None:
        sid, _, signature = value.partition(".")
        if not sid or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(sid)):
            return None
        return sid
