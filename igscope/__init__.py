"""igscope - a minimal, importable Instagram OSINT module.

A stripped-down fork of Osintgram with no web UI, no AI/LLM layer, no HikerAPI
key and no development scaffolding. One data source (the instagrapi backend,
authenticated with your own account's saved session), one simple class.

Quick start
-----------
    from igscope import InstagramClient

    ig = InstagramClient()                       # loads config/instagrapi_session.json
    data = ig.query("natgeo", "profile")         # dict of JSON-able data
    for part in ig.stream("natgeo", "content"):  # or stream capability-by-capability
        ...

First-time setup (run once)
---------------------------
    import igscope
    igscope.login("my_account", "my_password")        # or
    igscope.login_by_sessionid("<sessionid cookie>")
"""
from .backend import InstagrapiBackendError
from .client import InstagramClient, login, login_by_sessionid
from .service import (
    BackendError,
    InstagramService,
    OsintServiceError,
    PrivateProfileError,
    RequestLimitReachedError,
    TargetNotFoundError,
)

__version__ = "0.1.0"

__all__ = [
    "InstagramClient",
    "InstagramService",
    "login",
    "login_by_sessionid",
    "OsintServiceError",
    "TargetNotFoundError",
    "PrivateProfileError",
    "BackendError",
    "RequestLimitReachedError",
    "InstagrapiBackendError",
]
