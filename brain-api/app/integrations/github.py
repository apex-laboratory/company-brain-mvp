"""GitHub integration — GitHub App auth + polling + webhooks (KAN-2 / KAN-7).

Unlike Notion (OAuth user token, no webhooks), GitHub is a **GitHub App**:

* Auth is an App JWT (RS256, signed with the App private key) exchanged for a
  per-installation access token that expires in ~1 hour. There is no refresh
  token — you *re-mint* from ``App JWT + installation_id``. We store the
  ``installation_id`` as the connection's "refresh credential" so the generic
  ``source_sync`` refresh path (``integration.refresh(...)``) re-mints
  transparently.
* The install callback carries an ``installation_id`` (not a ``code``).
* Polling uses the installation's repositories: ``fetch_since`` ignores the
  synthetic channel handed in by ``source_sync`` (exactly like Notion's global
  ``/search``), enumerates ``/installation/repositories``, and pulls issues/PRs
  ``updated`` since the connection cursor. The cursor is the max ``updated_at``.
* Webhooks are real: ``verify_webhook`` validates the ``X-Hub-Signature-256``
  HMAC over the raw body. Webhook payloads always carry ``installation.id`` for
  routing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from jose import jwt

from app.config.settings import settings
from app.integrations.base import (
    ChannelRef,
    ConnectorAuthError,
    OAuthTokens,
    RawEvent,
    RawItem,
    http_client,
)
from app.shared.helpers.crypto import constant_time_compare

log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_INSTALL_BASE = "https://github.com/apps"
_PER_PAGE = 100
_JWT_TTL = 540  # seconds; GitHub caps App JWT lifetime at 10 min, stay under it.
_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``...Z``) into an aware UTC datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_rate_limited(response: httpx.Response) -> bool:
    """True when a 403/429 is a rate limit (vs a genuine permission error).

    GitHub overloads 403 for both rate limiting and missing permissions; it signals
    the rate-limit case with an exhausted ``x-ratelimit-remaining`` or a ``retry-after``
    header. httpx header keys are case-insensitive.
    """
    return (
        response.status_code == 429
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Starlette Headers are already insensitive;
    a plain ``dict`` in tests is not)."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _unwrap(payload: dict) -> tuple[dict | None, str]:
    """Resolve a payload to its ``(core_object, event_type)``.

    Handles both shapes: a webhook envelope ``{action, issue|pull_request|comment,
    ...}`` and a bare issue/PR object returned by the polling ``/issues`` endpoint.
    Returns ``(None, "")`` for payloads with no ingestible content (e.g. ``ping``).
    """
    # Bare object from the polling /issues endpoint: it carries its own id + content,
    # and — when it is a PR — a `pull_request` *stub* (just URLs, no id/title). Check
    # this FIRST so that stub isn't mistaken for a webhook envelope's nested object.
    if "id" in payload and ("title" in payload or "body" in payload):
        return payload, "pr" if payload.get("pull_request") else "issue"
    # Webhook envelope: {action, issue|pull_request|comment, ...}. ``comment`` is checked
    # FIRST because an issue_comment / pull_request_review_comment delivery carries BOTH
    # the comment *and* its parent issue/PR — resolving to the issue would silently drop
    # the comment's text and author.
    for key, etype in (("comment", "comment"), ("issue", "issue"), ("pull_request", "pr")):
        core = payload.get(key)
        if isinstance(core, dict):
            # An `issues` webhook represents a PR as an issue with a pull_request
            # sub-object — classify those as PRs.
            if key == "issue" and core.get("pull_request"):
                return core, "pr"
            return core, etype
    return None, ""


class GitHubIntegration:
    provider = "github"
    # Issue/PR comment chains are a discussion thread → the decision_identifier
    # runs its LLM pass to find authoritative moments.
    threaded = True

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", **_BASE_HEADERS}

    def _app_jwt(self) -> str:
        """Sign a short-lived RS256 App JWT (``iss`` = App id) with the App key."""
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + _JWT_TTL, "iss": settings.github_app_id}
        # The PEM is stored \n-escaped on one env line; restore real newlines.
        key = settings.github_app_private_key.replace("\\n", "\n")
        return jwt.encode(payload, key, algorithm="RS256")

    async def _mint_installation_token(self, installation_id: str) -> dict:
        """Exchange the App JWT for a fresh installation access token (~1h)."""
        resp = await http_client().post(
            f"{_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}", **_BASE_HEADERS},
        )
        resp.raise_for_status()
        return resp.json()

    # ── OAuth / install ──────────────────────────────────────────────────────────
    def authorize_url(
        self, state: str, redirect_uri: str, *, config: Mapping[str, str] | None = None
    ) -> str:
        # ``config`` is unused — GitHub App installs use a global install page.
        # GitHub App installs go through the App's install page; the callback URL is
        # configured on the App itself, so redirect_uri is not sent here.
        query = urlencode({"state": state})
        return f"{_INSTALL_BASE}/{settings.github_app_slug}/installations/new?{query}"

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        """Bind a GitHub App installation to the connecting workspace.

        SECURITY: the ``installation_id`` in the callback is attacker-influenceable
        (small, enumerable integers) and the App JWT will mint a token for *any*
        installation, so we must prove the caller actually controls this installation
        before minting. We do that with the "Request user authorization (OAuth) during
        installation" leg: the callback also carries a ``code``, which we exchange for a
        *user* token and check the installation appears in ``GET /user/installations``.
        Without this check an admin could bind a victim org's installation to their own
        workspace and ingest its private repos.
        """
        if not installation_id:
            raise ValueError("GitHub callback missing installation_id")
        await self._verify_installation_owner(code, installation_id)
        return await self._mint_tokens(installation_id)

    async def _mint_tokens(self, installation_id: str) -> OAuthTokens:
        """Mint a fresh installation access token (no ownership check — internal/refresh)."""
        data = await self._mint_installation_token(installation_id)
        return OAuthTokens(
            access_token=data["token"],
            # Store the installation_id as the re-mint credential (see module docstring).
            refresh_token=installation_id,
            expires_at=_parse_ts(data.get("expires_at")),
            external_account_id=installation_id,
            scopes=[],
            raw=data,
        )

    async def _verify_installation_owner(self, code: str, installation_id: str) -> None:
        """Confirm the OAuth user controls ``installation_id`` (cross-tenant guard).

        Requires the App to be configured for "Request user authorization (OAuth) during
        installation" and its OAuth client id/secret to be set. Raises ``ConnectorAuthError``
        when the code is missing, the user token can't be obtained, or the installation is
        not one the user can access.
        """
        if not settings.github_app_client_id or not settings.github_app_client_secret:
            raise ConnectorAuthError(
                "GitHub App OAuth client is not configured; cannot verify installation "
                "ownership. Set GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET and enable "
                "user authorization during installation."
            )
        if not code:
            raise ConnectorAuthError("GitHub callback missing user-authorization code.")

        token_resp = await http_client().post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_app_client_id,
                "client_secret": settings.github_app_client_secret,
                "code": code,
            },
        )
        token_resp.raise_for_status()
        user_token = token_resp.json().get("access_token")
        if not user_token:
            raise ConnectorAuthError("GitHub user-authorization code exchange failed.")

        target = str(installation_id)
        page = 1
        while True:
            resp = await http_client().get(
                f"{_API_BASE}/user/installations",
                headers=self._headers(user_token),
                params={"per_page": _PER_PAGE, "page": page},
            )
            resp.raise_for_status()
            installations = resp.json().get("installations", [])
            if any(str(inst.get("id")) == target for inst in installations):
                return
            if len(installations) < _PER_PAGE:
                break
            page += 1
        raise ConnectorAuthError(
            "GitHub installation is not accessible to the authorizing user "
            "(possible cross-tenant attempt)."
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        # ``refresh_token`` is the installation_id; re-mint a fresh installation token.
        # No ownership check here — ownership was verified at connect time; this is our
        # own stored credential being renewed.
        return await self._mint_tokens(refresh_token)

    async def revoke(self, access_token: str) -> None:
        # Nothing to revoke: an installation token is not revocable and expires on its
        # own (~1h). Ending access means removing the *installation* — see uninstall().
        return None

    async def uninstall(self, external_account_id: str) -> None:
        """Delete the App installation so it stops appearing on the user's repos.

        Optional connector capability (see ``base.SourceIntegration.revoke``); GitHub
        is the only provider that needs one, because a GitHub App install outlives
        every token it mints. Without this, disconnecting removed our row and left the
        App sitting on the repositories with no sign it had been disconnected.

        Authenticated with the **App JWT**, not an installation token: the endpoint
        acts on behalf of the App itself, and the installation token it would issue is
        exactly what is being destroyed.

        ``SourcesService.disconnect`` calls this only when the last workspace holding
        the installation disconnects, and swallows failures — a GitHub-side error must
        not block the local disconnect.
        """
        resp = await http_client().delete(
            f"{_API_BASE}/app/installations/{external_account_id}",
            headers={"Authorization": f"Bearer {self._app_jwt()}", **_BASE_HEADERS},
        )
        # 404 means it is already gone (uninstalled from GitHub's own settings page) —
        # the desired end state, so treat it as success rather than an error to log.
        if resp.status_code != 404:
            resp.raise_for_status()
        log.info("github: uninstalled App installation %s", external_account_id)

    # ── fetch ────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """List the repositories this installation can access."""
        channels: list[ChannelRef] = []
        page = 1
        while True:
            resp = await http_client().get(
                f"{_API_BASE}/installation/repositories",
                headers=self._headers(access_token),
                params={"per_page": _PER_PAGE, "page": page},
            )
            resp.raise_for_status()
            repos = resp.json().get("repositories", [])
            for repo in repos:
                channels.append(
                    ChannelRef(external_id=repo["full_name"], name=repo["full_name"])
                )
            if len(repos) < _PER_PAGE:
                break
            page += 1
        return channels

    async def _fetch_repo_issues(
        self, access_token: str, full_name: str, cursor: str | None
    ) -> list[RawItem]:
        """Fetch every issue/PR page for one repo updated since ``cursor``."""
        items: list[RawItem] = []
        page = 1
        while True:
            params: dict = {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": _PER_PAGE,
                "page": page,
            }
            if cursor:
                params["since"] = cursor
            resp = await http_client().get(
                f"{_API_BASE}/repos/{full_name}/issues",
                headers=self._headers(access_token),
                params=params,
            )
            resp.raise_for_status()
            batch = resp.json()
            items.extend(RawItem(external_id=str(o["id"]), payload=o) for o in batch)
            if len(batch) < _PER_PAGE:
                break
            page += 1
        return items

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch issues + PRs updated after ``cursor`` across all installation repos.

        ``channel`` is the synthetic connection-level channel from ``source_sync`` and
        is ignored — GitHub enumerates its own repos. Dedupe is handled downstream by
        the ``source_events`` unique constraint, so we collect everything the ``since``
        filter returns and advance the cursor to the newest ``updated_at`` seen.

        Per-repo errors are isolated so one bad repo can't fail the whole sweep, but
        because the cursor is connection-level we must not advance past a repo we
        skipped transiently (that would lose its items). Failures are classified:

        * ``401`` — re-raised; the token is broken, so ``source_sync`` marks the
          connection ``error`` (re-auth needed).
        * ``404`` / ``410`` / permission ``403`` — skipped *permanently* (issues
          disabled, archived, or not granted); these repos have nothing to give, so
          the cursor still advances.
        * ``429`` / rate-limit ``403`` / ``5xx`` — *transient*; the cursor is held so
          the next sync retries. Idempotent inserts make re-fetching succeeded repos
          harmless.
        """
        items: list[RawItem] = []
        newest = _parse_ts(cursor)
        incomplete = False  # a transient failure means "don't advance the cursor"

        for repo in await self.list_channels(access_token):
            try:
                repo_items = await self._fetch_repo_issues(
                    access_token, repo.external_id, cursor
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401:
                    raise  # whole-connection auth failure — let source_sync handle it
                if status == 429 or (status == 403 and _is_rate_limited(exc.response)):
                    log.warning("github fetch: rate limited on %s — will retry", repo.external_id)
                    incomplete = True
                elif status in (403, 404, 410):
                    log.warning(
                        "github fetch: skipping %s (%s — disabled/archived/no access)",
                        repo.external_id, status,
                    )
                elif 500 <= status < 600:
                    log.warning(
                        "github fetch: server error on %s (%s) — will retry",
                        repo.external_id, status,
                    )
                    incomplete = True
                else:
                    raise  # unexpected status — surface it
                continue

            for item in repo_items:
                items.append(item)
                edited = _parse_ts(item.payload.get("updated_at"))
                if edited is not None and (newest is None or edited > newest):
                    newest = edited

        if incomplete:
            return items, cursor  # hold the cursor; next sync re-fetches everything
        return items, (newest.isoformat() if newest else cursor)

    # ── webhooks ───────────────────────────────────────────────────────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        signature = _header(headers, "X-Hub-Signature-256")
        if not signature or not secret:
            return False
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return constant_time_compare(expected.encode("utf-8"), signature.encode("utf-8"))

    # ── normalize ────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        core, event_type = _unwrap(item.payload)
        if core is None:
            # e.g. a `ping` delivery — webhook_ingest skips on ValueError.
            raise ValueError("Unsupported GitHub payload (no issue/PR/comment)")

        user = core.get("user") or item.payload.get("sender") or {}
        updated = core.get("updated_at") or core.get("created_at")
        title = core.get("title") or ""
        body = core.get("body") or ""
        return RawEvent(
            provider="github",
            source_id=str(core.get("id", "")),
            # Dedupe key: object id + update time so a re-edit produces a fresh event.
            external_event_id=f"{core.get('id')}:{updated}",
            event_type=event_type,
            actor={"id": str(user.get("id", "")), "email": "", "name": user.get("login", "")},
            content=f"{title}\n\n{body}".strip(),
            created_at=_parse_ts(updated) or datetime.now(UTC),
            url=core.get("html_url", ""),
            raw=item.payload,
        )
