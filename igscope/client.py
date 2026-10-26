"""The simple front door: build a client once, then run requests against a
target username and get JSON back - all at once, or as a stream.

    from igscope import InstagramClient

    ig = InstagramClient()                      # loads your saved session

    data = ig.query("natgeo", "profile")        # dict, everything at once
    data = ig.query("natgeo", "followers", limit=50)

    for part in ig.stream("natgeo", "content"): # one capability at a time
        print(part["capability"], part["ok"])

    async for part in ig.astream("natgeo", "content"):   # async stream
        ...

A "request" is either a *group* (profile / network / content / contacts /
search) that runs several capabilities, or the name of a single capability
(see InstagramClient.capabilities()).
"""
import asyncio
from typing import AsyncIterator, Iterator, Optional

from .backend import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_SESSION_PATH,
    InstagrapiBackendError,
    InstagrapiClient,
    resolve_credentials,
    session_exists,
)
from .service import InstagramService, OsintServiceError

__all__ = ["InstagramClient", "login", "login_by_sessionid"]


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------
# Each capability is a small adapter (service, params) -> JSON-able value.
# Pulling params out of a dict (rather than **kwargs) keeps every capability
# tolerant of extra params it doesn't care about, so one params dict can be
# passed to a whole group.
#
# Entry: name -> (func, needs_target, required_param_or_None)

def _int_or_none(value):
    """None / "all" / "" mean "no limit"; anything else is coerced to int."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "all", "none"):
        return None
    return int(value)


CAPABILITIES = {
    # ---- profile ----
    "user_info":        (lambda s, p: s.get_user_info(), True, None),
    "profile_picture":  (lambda s, p: s.get_user_propic(), True, None),

    # ---- network ----
    "followers":        (lambda s, p: s.get_followers(limit=_int_or_none(p.get("limit", 200))), True, None),
    "followings":       (lambda s, p: s.get_followings(limit=_int_or_none(p.get("limit", 200))), True, None),
    "compare":          (lambda s, p: s.compare_with(p["other"], p.get("scope", "both"),
                                                     _int_or_none(p.get("limit", 500))), True, "other"),

    # ---- content (feed-based; pass limit_posts to cap the scan) ----
    "posts":              (lambda s, p: s.get_posts(_int_or_none(p.get("limit_posts"))), True, None),
    "hashtags":           (lambda s, p: s.get_hashtags(_int_or_none(p.get("limit_posts"))), True, None),
    "captions":           (lambda s, p: s.get_captions(_int_or_none(p.get("limit_posts"))), True, None),
    "likes":              (lambda s, p: s.get_total_likes(_int_or_none(p.get("limit_posts"))), True, None),
    "comments":           (lambda s, p: s.get_total_comments(_int_or_none(p.get("limit_posts"))), True, None),
    "media_types":        (lambda s, p: s.get_media_type(_int_or_none(p.get("limit_posts"))), True, None),
    "posting_times":      (lambda s, p: s.get_posting_times(_int_or_none(p.get("limit_posts"))), True, None),
    "locations":          (lambda s, p: s.get_addrs(_int_or_none(p.get("limit_posts"))), True, None),
    "photo_descriptions": (lambda s, p: s.get_photo_descriptions(_int_or_none(p.get("limit_posts"))), True, None),
    "stories":            (lambda s, p: s.get_user_stories(), True, None),
    "highlights":         (lambda s, p: s.get_highlights(), True, None),
    "photos":             (lambda s, p: s.get_user_photos(_int_or_none(p.get("limit"))), True, None),
    "comment_data":       (lambda s, p: s.get_comment_data(_int_or_none(p.get("limit_posts"))), True, None),
    "people_who_commented": (lambda s, p: s.get_people_who_commented(_int_or_none(p.get("limit_posts"))), True, None),
    "people_who_tagged":  (lambda s, p: s.get_people_who_tagged(_int_or_none(p.get("limit"))), True, None),
    "people_tagged_by_user": (lambda s, p: s.get_people_tagged_by_user(_int_or_none(p.get("limit_posts"))), True, None),
    "post_comments":      (lambda s, p: s.get_post_comments(p["post"], _int_or_none(p.get("limit"))), False, "post"),

    # ---- contacts (one lookup per candidate - expensive) ----
    "followers_email":  (lambda s, p: s.get_followers_email(_int_or_none(p.get("match_limit", 20)),
                                                            _int_or_none(p.get("max_checks", 100))), True, None),
    "followings_email": (lambda s, p: s.get_followings_email(_int_or_none(p.get("match_limit", 20)),
                                                            _int_or_none(p.get("max_checks", 100))), True, None),
    "followers_phone":  (lambda s, p: s.get_followers_phone(_int_or_none(p.get("match_limit", 20)),
                                                            _int_or_none(p.get("max_checks", 100))), True, None),
    "followings_phone": (lambda s, p: s.get_followings_phone(_int_or_none(p.get("match_limit", 20)),
                                                            _int_or_none(p.get("max_checks", 100))), True, None),

    # ---- general search (no target needed) ----
    "hashtag_search":   (lambda s, p: s.search_hashtag(p["hashtag"], p.get("sort", "top"),
                                                       _int_or_none(p.get("limit", 30))), False, "hashtag"),
    "location_search":  (lambda s, p: s.search_location(p["place"], _int_or_none(p.get("limit", 30))), False, "place"),
}

# A "request" group expands to a list of capability names. Search is handled
# specially: only the capabilities whose required param was supplied run.
GROUPS = {
    "profile":  ["user_info", "profile_picture"],
    "network":  ["followers", "followings"],
    "content":  ["posts", "hashtags", "captions", "likes", "comments", "media_types",
                 "posting_times", "locations", "stories", "highlights", "photo_descriptions"],
    "contacts": ["followers_email", "followings_email", "followers_phone", "followings_phone"],
    "search":   ["hashtag_search", "location_search"],
}


class InstagramClient:
    """Authenticate once, then run requests against any target."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        session_path: str = DEFAULT_SESSION_PATH,
        credentials_path: str = DEFAULT_CREDENTIALS_PATH,
    ):
        """Load the saved instagrapi session. `username`/`password` default to
        env vars (INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD) or the [Credentials]
        section of `credentials_path`; they are only a fallback - a valid
        session file is what's actually used.
        """
        if username is None and password is None:
            username, password = resolve_credentials(credentials_path)
        if not session_exists(session_path):
            raise InstagrapiBackendError(
                f"No saved session at {session_path}. Create one once with "
                "igscope.login(user, pass) or igscope.login_by_sessionid(sid)."
            )
        self._backend = InstagrapiClient(username, password, session_path)

    @property
    def logged_in_as(self) -> Optional[str]:
        return self._backend.logged_in_as

    @staticmethod
    def groups() -> list:
        """The request groups (profile, network, content, contacts, search)."""
        return list(GROUPS)

    @staticmethod
    def capabilities() -> list:
        """Every individual capability name usable as a request."""
        return list(CAPABILITIES)

    # ---- resolving a request to a list of capabilities ----

    @staticmethod
    def _resolve(request: str, params: dict) -> list:
        if request in CAPABILITIES:
            return [request]
        if request not in GROUPS:
            raise OsintServiceError(
                f"Unknown request {request!r}. Use a group {list(GROUPS)} "
                f"or a capability (InstagramClient.capabilities())."
            )
        caps = GROUPS[request]
        if request == "search":
            # Only run the searches the caller actually gave params for.
            caps = [c for c in caps if CAPABILITIES[c][2] in params]
            if not caps:
                raise OsintServiceError(
                    "The 'search' request needs hashtag=... and/or place=... .")
        return caps

    def _build_service(self, target: Optional[str], max_calls: Optional[int]) -> InstagramService:
        return InstagramService(target, self._backend, max_calls=max_calls)

    # ---- synchronous: everything at once ----

    def query(
        self,
        target: Optional[str],
        request: str = "profile",
        *,
        max_calls: Optional[int] = None,
        **params,
    ) -> dict:
        """Run `request` against `target` and return one JSON-able dict with a
        "results" section (capability name -> data, or {"error": ...}) plus
        metadata. `target` may be None for the 'search' request.

        `max_calls` caps the real backend requests this whole query may make.
        Extra keyword args (limit, limit_posts, hashtag, place, other, ...) are
        passed through to the capabilities that understand them.
        """
        caps = self._resolve(request, params)
        service = self._build_service(target, max_calls)
        results = {}
        for name in caps:
            func = CAPABILITIES[name][0]
            try:
                results[name] = {"ok": True, "data": func(service, params)}
            except Exception as e:  # noqa: BLE001 - one failed capability must not sink the rest
                results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {
            "target": target,
            "request": request,
            "backend": service.backend_name,
            "api_calls": service.api_call_count,
            "results": results,
        }

    # ---- synchronous stream: one capability at a time ----

    def stream(
        self,
        target: Optional[str],
        request: str = "profile",
        *,
        max_calls: Optional[int] = None,
        **params,
    ) -> Iterator[dict]:
        """Like query(), but yield one record per capability as it finishes:
        {"capability": name, "ok": bool, "data"/"error": ...}. The final record
        is a {"capability": "_summary", ...} with target/api_calls.
        """
        caps = self._resolve(request, params)
        service = self._build_service(target, max_calls)
        for name in caps:
            func = CAPABILITIES[name][0]
            try:
                yield {"capability": name, "ok": True, "data": func(service, params)}
            except Exception as e:  # noqa: BLE001
                yield {"capability": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        yield {"capability": "_summary", "ok": True, "target": target,
               "request": request, "backend": service.backend_name,
               "api_calls": service.api_call_count}

    # ---- async stream ----

    async def astream(
        self,
        target: Optional[str],
        request: str = "profile",
        *,
        max_calls: Optional[int] = None,
        **params,
    ) -> AsyncIterator[dict]:
        """Async version of stream(): the blocking backend work for each
        capability runs in a worker thread, so results arrive without blocking
        the event loop. Yields the same records as stream().
        """
        caps = self._resolve(request, params)
        service = self._build_service(target, max_calls)
        for name in caps:
            func = CAPABILITIES[name][0]
            try:
                data = await asyncio.to_thread(func, service, params)
                yield {"capability": name, "ok": True, "data": data}
            except Exception as e:  # noqa: BLE001
                yield {"capability": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        yield {"capability": "_summary", "ok": True, "target": target,
               "request": request, "backend": service.backend_name,
               "api_calls": service.api_call_count}

    async def aquery(
        self,
        target: Optional[str],
        request: str = "profile",
        *,
        max_calls: Optional[int] = None,
        **params,
    ) -> dict:
        """Async version of query(): the same dict, assembled off the event
        loop. Convenient when you just want the whole result in async code."""
        results = {}
        summary = {}
        async for part in self.astream(target, request, max_calls=max_calls, **params):
            if part["capability"] == "_summary":
                summary = part
                continue
            name = part.pop("capability")
            results[name] = part
        return {
            "target": target,
            "request": request,
            "backend": summary.get("backend", "instagrapi"),
            "api_calls": summary.get("api_calls"),
            "results": results,
        }


# ---------------------------------------------------------------------------
# One-time session setup (run once, then just construct InstagramClient)
# ---------------------------------------------------------------------------

def login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    verification_code: str = "",
    session_path: str = DEFAULT_SESSION_PATH,
    credentials_path: str = DEFAULT_CREDENTIALS_PATH,
) -> str:
    """Log in with username/password and save the session to `session_path`.

    Run this once from an interactive terminal (a 2FA/challenge prompt is
    answered via input()). Credentials fall back to env vars / credentials.ini.
    Returns the username the session belongs to.
    """
    from pathlib import Path

    from instagrapi import Client

    if username is None and password is None:
        username, password = resolve_credentials(credentials_path)
    if not username or not password:
        raise OsintServiceError(
            "No credentials: pass username/password, set INSTAGRAM_USERNAME/"
            "INSTAGRAM_PASSWORD, or fill [Credentials] in credentials.ini.")

    cl = Client()
    cl.challenge_code_handler = lambda user, choice: input(
        f"Enter the verification code sent to {user} ({choice}): ").strip()
    cl.login(username, password, verification_code=verification_code)
    out = Path(session_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(out)
    return cl.username


def login_by_sessionid(sessionid: str, session_path: str = DEFAULT_SESSION_PATH) -> str:
    """Save a session from a browser `sessionid` cookie (no password needed).
    Returns the username the session belongs to."""
    from pathlib import Path

    from instagrapi import Client

    sid = (sessionid or "").strip()
    # Repair an accidental double-paste of the cookie value.
    if len(sid) > 60 and len(sid) % 2 == 0 and sid[: len(sid) // 2] == sid[len(sid) // 2:]:
        sid = sid[: len(sid) // 2]
    cl = Client()
    cl.login_by_sessionid(sid)
    out = Path(session_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(out)
    return cl.username
