# igscope

A minimal, importable Instagram OSINT module — a stripped-down fork of
Osintgram. No web UI, no AI/LLM layer, no HikerAPI key, no dev scaffolding.
Just your own Instagram account's saved session and one simple class.

```python
from igscope import InstagramClient

ig = InstagramClient()                       # loads config/instagrapi_session.json
data = ig.query("natgeo", "profile")         # -> a JSON-able dict, all at once
```

## Install

```bash
pip install -r igscope/requirements.txt      # instagrapi (+ geopy for 'locations')
```

## One-time setup: save a session

igscope authenticates as **your own** Instagram account (use a secondary
account, not your main one). You log in **once** to produce a session file; after
that every run just reuses it.

```python
import igscope

# Option A — username + password (answer any 2FA prompt in the terminal):
igscope.login("my_account", "my_password")

# Option B — paste a browser `sessionid` cookie (no password):
igscope.login_by_sessionid("<sessionid cookie value>")
```

Credentials can also come from env vars (`INSTAGRAM_USERNAME`,
`INSTAGRAM_PASSWORD`) or `config/credentials.ini`:

```ini
[Credentials]
username = my_account
password = my_password
```

The session is saved to `config/instagrapi_session.json` by default (override
with `session_path=`).

## Usage

```python
from igscope import InstagramClient
ig = InstagramClient()
```

### Synchronous — get everything at once

```python
data = ig.query("natgeo", "profile")
data = ig.query("natgeo", "network", limit=100)
data = ig.query("natgeo", "content", limit_posts=20, max_calls=50)
```

`query()` returns:

```python
{
  "target": "natgeo",
  "request": "profile",
  "backend": "instagrapi",
  "api_calls": 3,
  "results": {
    "user_info":       {"ok": True,  "data": {...}},
    "profile_picture": {"ok": True,  "data": {"url": "..."}},
  },
}
```

One failing capability never sinks the rest — it just gets
`{"ok": False, "error": "..."}`.

### Asynchronous stream — one capability at a time

```python
async for part in ig.astream("natgeo", "content", limit_posts=20):
    # part == {"capability": "hashtags", "ok": True, "data": [...]}
    ...
```

There's also a synchronous generator `ig.stream(...)` and an async
`ig.aquery(...)` that returns the whole dict like `query()`.

## Requests

A **request** is either a *group* or a single *capability*.

| Group      | Runs |
|------------|------|
| `profile`  | `user_info`, `profile_picture` |
| `network`  | `followers`, `followings` |
| `content`  | `posts`, `hashtags`, `captions`, `likes`, `comments`, `media_types`, `posting_times`, `locations`, `stories`, `highlights`, `photo_descriptions` |
| `contacts` | `followers_email`, `followings_email`, `followers_phone`, `followings_phone` |
| `search`   | `hashtag_search` and/or `location_search` (whichever params you pass) |

Any single capability can also be used as the request directly:

```python
ig.query("natgeo", "followers", limit=50)
ig.query("natgeo", "posting_times", limit_posts=100)
ig.query("natgeo", "compare", other="instagram", scope="followings")
ig.query(None,     "hashtag_search", hashtag="sunset", sort="recent", limit=20)
ig.query(None,     "location_search", place="Berlin")
ig.query(None,     "post_comments", post="https://www.instagram.com/p/ABC123/")
```

`InstagramClient.groups()` and `InstagramClient.capabilities()` list them all.

### Common parameters

- `limit` — followers/followings/search result cap.
- `limit_posts` — how many feed posts to scan for `content` capabilities
  (default: all — set this to keep big accounts fast and cheap).
- `max_calls` — hard cap on real backend requests for the whole query.
- `match_limit` / `max_checks` — for contacts: how many matches to collect /
  candidates to inspect.
- `other`, `scope` — for `compare`. `hashtag`, `place`, `sort` — for search.
- `post` — URL / shortcode / id for `post_comments`.

## Notes

- Everything returned is plain JSON-serializable data (`dict` / `list`) — pass
  it straight to `json.dumps(...)`.
- The `locations` capability reverse-geocodes via Nominatim and needs `geopy`.
- Rate limits are Instagram's; `max_calls` and `limit_posts` are your throttle.
- Use responsibly and lawfully — only against data you're permitted to access.
