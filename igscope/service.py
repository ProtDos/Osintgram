"""Side-effect-free OSINT capabilities over a single Instagram account.

Every method returns a plain, JSON-serializable Python structure (dict / list)
- it never prints, writes files, or blocks on input(). `InstagramService` is
constructed around one target username; each capability answers one question
about that target (or, for the search_* capabilities, about a hashtag/place
that belongs to no account).

This is a stripped-down fork of Osintgram's service layer: HikerAPI, the web
UI, the LLM/AI layer and all the request-budget/cancellation plumbing have been
removed. The one data source is the instagrapi backend (see backend.py).
"""
import itertools
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx


class OsintServiceError(Exception):
    """Base class for service errors."""


class TargetNotFoundError(OsintServiceError):
    """The target Instagram username does not exist."""


class PrivateProfileError(OsintServiceError):
    """The target is private and the logged-in account can't see it."""


class BackendError(OsintServiceError):
    """The backend returned an error or the request failed."""


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------
# Some Instagram endpoints prefix numeric fields with a type tag ("1ltaken_at",
# "1flat"). normalize_payload strips those back to the plain field names every
# capability below reads. It is idempotent on already-plain (instagrapi) data.
_TYPED_KEY_PREFIX = re.compile(r"^1[a-z](?=[a-z_])")


def normalize_payload(value):
    if isinstance(value, list):
        return [normalize_payload(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            plain = _TYPED_KEY_PREFIX.sub("", key)
            item = normalize_payload(item)
            if plain in out and out[plain] is not None and item is None:
                continue
            out[plain] = item
        if "pk" not in out and "id" in out and "username" in out:
            out["pk"] = out["id"]
        return out
    return value


# ---------------------------------------------------------------------------
# Transient-failure retries
# ---------------------------------------------------------------------------
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.6
_TRANSIENT_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_TRANSIENT_DETAIL = re.compile(r"\b(429|5\d\d)\b|rate.?limit|too many requests|timed? ?out|temporar", re.I)


def _error_detail(result) -> Optional[str]:
    if isinstance(result, dict):
        detail = result.get("error") or result.get("detail")
        if detail:
            return str(detail)
    return None


def _transient_error_detail(result) -> Optional[str]:
    detail = _error_detail(result)
    return detail if detail and _TRANSIENT_DETAIL.search(detail) else None


class _CountingApiProxy:
    """Transparent wrapper that counts every backend call and retries the
    transient ones (dropped connection, momentary 429/5xx).

    A single capability can make many real requests (pagination pages, one
    user lookup per follower checked for contact info); `call_count` is the
    true cost signal, and `max_calls`, if set, caps real requests for the
    lifetime of this proxy so one query can't run away.
    """

    def __init__(self, api_client, max_calls: Optional[int] = None):
        self._wrapped = api_client
        self.call_count = 0
        self.max_calls = max_calls

    def __getattr__(self, name):
        attr = getattr(self._wrapped, name)
        if not callable(attr):
            return attr

        def _counted(*args, **kwargs):
            if self.max_calls is not None and self.call_count >= self.max_calls:
                raise RequestLimitReachedError(
                    f"Reached this query's limit of {self.max_calls} backend requests."
                )
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                last = attempt == RETRY_ATTEMPTS
                self.call_count += 1
                try:
                    result = attr(*args, **kwargs)
                except _TRANSIENT_EXCEPTIONS:
                    if last:
                        raise
                    time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
                    continue
                transient = _transient_error_detail(result)
                if transient and not last:
                    time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
                    continue
                return result

        return _counted


class RequestLimitReachedError(OsintServiceError):
    """The per-query cap on real backend requests was hit. Capability methods
    catch this mid-scan and return whatever they gathered so far."""


class InstagramService:
    """Wrapper around one target's Instagram data.

    `api_client` is a backend.InstagrapiClient (or anything implementing the
    same methods). `max_calls` optionally caps real backend requests for this
    service's lifetime.
    """

    def __init__(self, target: Optional[str], api_client, max_calls: Optional[int] = None):
        self._backend_client = api_client
        self.api = _CountingApiProxy(api_client, max_calls=max_calls)
        # `target` may be None: hashtag/place searches belong to no account.
        self.target = target
        self.user = self._fetch_user(target) if target else {}
        self.target_id = self.user.get("pk")
        self.is_private = bool(self.user.get("is_private"))

    @property
    def backend_name(self) -> str:
        return "instagrapi"

    @property
    def api_call_count(self) -> int:
        return self.api.call_count

    def _fetch_user(self, username: str) -> dict:
        try:
            data = self.api.user_by_username_v2(username)
        except Exception as e:
            raise BackendError(str(e)) from e
        if "error" in data:
            raise BackendError(str(data["error"]))
        if "detail" in data:
            raise TargetNotFoundError(f"@{username} not found: {data['detail']}")
        return data["user"]

    def _require_target(self) -> None:
        if not self.target_id:
            raise OsintServiceError("This request needs a target username.")

    def _require_public(self) -> None:
        # The logged-in instagrapi account can usually read private accounts it
        # follows, so there is no hard public-only guard here - this just
        # ensures a target was given.
        self._require_target()

    # ---- pagination helpers ----

    def _iter_items(self, api_method, results_key: str, cursor_param: str = "page_id", user_id=None):
        subject = self.target_id if user_id is None else user_id
        next_page_id = None
        while True:
            try:
                result = api_method(subject, **({cursor_param: next_page_id} if next_page_id else {}))
            except RequestLimitReachedError:
                return
            yield from normalize_payload(result.get("response", {}).get(results_key, []))
            next_page_id = result.get("next_page_id")
            if not next_page_id:
                return

    def _paginate(self, api_method, results_key: str, limit: Optional[int] = None,
                  cursor_param: str = "page_id", user_id=None) -> list:
        return list(itertools.islice(self._iter_items(api_method, results_key, cursor_param, user_id), limit))

    def _get_feed(self, limit: Optional[int] = None) -> list:
        return self._paginate(self.api.user_medias_g2, "items", limit=limit, cursor_param="next_page_id")

    def _get_comments(self, media_id, limit: Optional[int] = None) -> list:
        data = []
        next_page_id = ""
        while True:
            try:
                result = self.api.media_comments_v2(media_id, page_id=next_page_id)
            except RequestLimitReachedError:
                return data
            except Exception as e:
                if "Entries not found" in str(e):
                    return data
                raise BackendError(str(e)) from e
            data.extend(result.get("response", {}).get("comments", []))
            next_page_id = result.get("next_page_id")
            if limit is not None and len(data) >= limit:
                return data[:limit]
            if not next_page_id:
                break
        return data

    @staticmethod
    def _summarize_user(user: dict) -> dict:
        return {
            "id": user["pk"],
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
        }

    _MEDIA_TYPE_NAMES = {1: "photo", 2: "video", 8: "carousel"}

    @staticmethod
    def _media_files(media: dict) -> dict:
        candidates = (media.get("image_versions2") or {}).get("candidates") or []
        sized = [c for c in candidates if (c.get("width") or 0) >= 320]
        thumbnail = min(sized, key=lambda c: c["width"]) if sized else (candidates[0] if candidates else {})
        largest = max(candidates, key=lambda c: c.get("width") or 0) if candidates else {}
        videos = media.get("video_versions") or []
        media_url = videos[0].get("url") if media.get("media_type") == 2 and videos else largest.get("url")
        return {
            "media_type": InstagramService._MEDIA_TYPE_NAMES.get(media.get("media_type"), "other"),
            "thumbnail_url": thumbnail.get("url"),
            "media_url": media_url,
        }

    @staticmethod
    def _post_preview(post: dict) -> dict:
        slides = [InstagramService._media_files(s) for s in post.get("carousel_media") or []]
        files = InstagramService._media_files(post)
        if slides and not files["thumbnail_url"]:
            files = {**slides[0], "media_type": files["media_type"]}
        code = post.get("code")
        path = "reel" if post.get("product_type") == "clips" else "p"
        caption = (post.get("caption") or {}).get("text") or ""
        return {
            "id": post.get("id"),
            **files,
            "slides": slides or None,
            "permalink": f"https://www.instagram.com/{path}/{code}/" if code else None,
            "like_count": post.get("like_count"),
            "comment_count": post.get("comment_count"),
            "play_count": InstagramService._play_count(post),
            "taken_at": post.get("taken_at"),
            "caption": caption[:120],
            "coauthors": InstagramService._coauthors(post),
            "paid_partnership": bool(post.get("is_paid_partnership")),
            "author": (post.get("user") or {}).get("username"),
        }

    @staticmethod
    def _play_count(post: dict) -> Optional[int]:
        return post.get("play_count") or post.get("ig_play_count") or post.get("view_count")

    @staticmethod
    def _coauthors(post: dict) -> list:
        return [u.get("username") for u in post.get("coauthor_producers") or [] if u.get("username")]

    # ---- profile ----

    _EXTRA_PROFILE_FIELDS = (
        "external_url", "public_email", "contact_phone_number", "public_phone_country_code",
        "public_phone_number", "whatsapp_number", "business_contact_method", "address_street",
        "city_name", "zip", "latitude", "longitude", "instagram_location_id", "category",
        "category_name", "business_category_name", "account_type", "is_memorialized", "fbid_v2",
        "total_clips_count", "total_igtv_videos", "usertags_count", "highlight_reel_count",
    )

    def get_user_info(self) -> dict:
        self._require_target()
        data = self.user
        info = {
            "id": data["pk"],
            "username": self.target,
            "full_name": data.get("full_name"),
            "biography": data.get("biography"),
            "follower_count": data.get("follower_count"),
            "following_count": data.get("following_count"),
            "media_count": data.get("media_count"),
            "is_private": data.get("is_private"),
            "is_business": data.get("is_business"),
            "is_verified": data.get("is_verified"),
            "profile_pic_url_hd": (data.get("hd_profile_pic_url_info") or {}).get("url"),
        }
        for key in self._EXTRA_PROFILE_FIELDS:
            if data.get(key) not in (None, "", [], {}):
                info[key] = data[key]
        links = [
            {"title": link.get("title") or None, "url": link.get("url") or link.get("lynx_url")}
            for link in (data.get("bio_links") or [])
            if isinstance(link, dict) and (link.get("url") or link.get("lynx_url"))
        ]
        if links:
            info["bio_links"] = links
        pronouns = [p for p in (data.get("pronouns") or []) if p]
        if pronouns:
            info["pronouns"] = "/".join(pronouns)
        return info

    def get_user_propic(self) -> dict:
        self._require_target()
        data = self.user
        info = data.get("hd_profile_pic_url_info")
        if info and info.get("url"):
            return {"url": info["url"]}
        versions = data.get("hd_profile_pic_versions") or []
        if versions:
            return {"url": versions[-1]["url"]}
        raise BackendError("No profile picture available")

    # ---- network ----

    def get_followers(self, limit: Optional[int] = 200) -> list:
        self._require_public()
        users = self._paginate(self.api.user_followers_g2, "users", limit=limit)
        return [self._summarize_user(u) for u in users]

    def get_followings(self, limit: Optional[int] = 200) -> list:
        self._require_public()
        users = self._paginate(self.api.user_following_g2, "users", limit=limit)
        return [self._summarize_user(u) for u in users]

    def compare_with(self, other_username: str, scope: str = "both", limit: Optional[int] = 500) -> dict:
        """Accounts in common with another public account: who follows both,
        and who both follow. `scope` is "followers", "followings" or "both"."""
        scope = (scope or "both").lower()
        if scope not in ("followers", "followings", "both"):
            raise OsintServiceError(f"invalid scope: {scope!r} (use followers, followings or both)")
        self._require_public()
        other = normalize_payload(self._fetch_user(other_username))
        other_id = other["pk"]

        def common(mine, theirs):
            by_pk = {u["pk"]: u for u in mine}
            return [self._summarize_user(by_pk[u["pk"]]) for u in theirs if u["pk"] in by_pk]

        result = {"target": self.target, "other": other.get("username") or other_username, "scope": scope}
        scanned = {}
        if scope in ("followers", "both"):
            mine = self._paginate(self.api.user_followers_g2, "users", limit=limit)
            theirs = self._paginate(self.api.user_followers_g2, "users", limit=limit, user_id=other_id)
            result["common_followers"] = common(mine, theirs)
            scanned["target_followers"] = len(mine)
            scanned["other_followers"] = len(theirs)
        if scope in ("followings", "both"):
            mine = self._paginate(self.api.user_following_g2, "users", limit=limit)
            theirs = self._paginate(self.api.user_following_g2, "users", limit=limit, user_id=other_id)
            result["common_followings"] = common(mine, theirs)
            scanned["target_followings"] = len(mine)
            scanned["other_followings"] = len(theirs)
        result["scanned"] = scanned
        return result

    # ---- content ----

    def get_posts(self, limit_posts: Optional[int] = None) -> list:
        """Compact preview of the target's posts (newest first)."""
        self._require_public()
        return [self._post_preview(p) for p in self._get_feed(limit=limit_posts)]

    def get_hashtags(self, limit_posts: Optional[int] = None) -> list:
        self._require_public()
        counter: dict = {}
        for post in self._get_feed(limit=limit_posts):
            caption = post.get("caption")
            if not caption:
                continue
            for word in (caption.get("text") or "").split():
                if word.startswith("#"):
                    counter[word] = counter.get(word, 0) + 1
        ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        return [{"hashtag": h, "count": c} for h, c in ranked]

    def get_captions(self, limit_posts: Optional[int] = None) -> list:
        self._require_public()
        captions = []
        for item in self._get_feed(limit=limit_posts):
            caption = item.get("caption")
            if caption and caption.get("text"):
                captions.append(caption["text"])
        return captions

    def get_total_comments(self, limit_posts: Optional[int] = None) -> dict:
        self._require_public()
        data = self._get_feed(limit=limit_posts)
        counts = [int(p.get("comment_count", 0)) for p in data]
        posts = len(counts)
        if posts == 0:
            return {"comment_counter": 0, "posts": 0, "min": 0, "max": 0, "avg": 0}
        total = sum(counts)
        return {"comment_counter": total, "posts": posts, "min": min(counts),
                "max": max(counts), "avg": total // posts}

    def get_total_likes(self, limit_posts: Optional[int] = None) -> dict:
        self._require_public()
        data = self._get_feed(limit=limit_posts)
        counts = [int(p.get("like_count", 0)) for p in data]
        posts = len(counts)
        if posts == 0:
            return {"like_counter": 0, "posts": 0, "min": 0, "max": 0, "avg": 0}
        total = sum(counts)
        return {"like_counter": total, "posts": posts, "min": min(counts),
                "max": max(counts), "avg": total // posts}

    def get_media_type(self, limit_posts: Optional[int] = None) -> dict:
        self._require_public()
        data = self._get_feed(limit=limit_posts)
        photos = sum(1 for p in data if p.get("media_type") == 1)
        videos = sum(1 for p in data if p.get("media_type") == 2)
        carousels = sum(1 for p in data if p.get("media_type") == 8)
        plays = [n for n in (self._play_count(p) for p in data if p.get("media_type") == 2) if n]
        return {
            "photos": photos, "videos": videos, "carousels": carousels, "total": len(data),
            "video_plays_total": sum(plays),
            "video_plays_avg": sum(plays) // len(plays) if plays else 0,
            "collaborations": sum(1 for p in data if self._coauthors(p)),
            "paid_partnerships": sum(1 for p in data if p.get("is_paid_partnership")),
        }

    def get_posting_times(self, limit_posts: Optional[int] = None) -> dict:
        """Per weekday/hour posting counts (UTC), busiest slot and frequency."""
        self._require_public()
        times = sorted(p["taken_at"] for p in self._get_feed(limit=limit_posts) if p.get("taken_at"))
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        by_weekday = dict.fromkeys(weekdays, 0)
        by_hour = {f"{h:02d}": 0 for h in range(24)}
        for t in times:
            when = datetime.fromtimestamp(t, tz=timezone.utc)
            by_weekday[weekdays[when.weekday()]] += 1
            by_hour[f"{when.hour:02d}"] += 1
        if not times:
            return {"posts": 0, "timezone": "UTC", "by_weekday": by_weekday, "by_hour": by_hour}
        busiest_hour = int(max(by_hour, key=by_hour.get))
        gaps = [(b - a) / 86400 for a, b in zip(times, times[1:])]
        return {
            "posts": len(times), "timezone": "UTC",
            "most_active_day": max(by_weekday, key=by_weekday.get),
            "most_active_hour": f"{busiest_hour:02d}:00-{(busiest_hour + 1) % 24:02d}:00",
            "first_post": datetime.fromtimestamp(times[0], tz=timezone.utc).strftime("%Y-%m-%d"),
            "last_post": datetime.fromtimestamp(times[-1], tz=timezone.utc).strftime("%Y-%m-%d"),
            "avg_days_between_posts": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "by_weekday": by_weekday, "by_hour": by_hour,
        }

    def get_addrs(self, limit_posts: Optional[int] = None) -> list:
        """Locations tagged on the target's posts, reverse-geocoded to an
        address. Needs the optional `geopy` dependency."""
        self._require_public()
        try:
            from geopy.extra.rate_limiter import RateLimiter
            from geopy.geocoders import Nominatim
        except ImportError as e:
            raise OsintServiceError(
                "get_addrs needs the optional 'geopy' dependency (pip install geopy)."
            ) from e
        geolocator = Nominatim(user_agent="igscope")
        reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1, max_retries=2,
                              swallow_exceptions=True)
        locations = {}
        for post in self._get_feed(limit=limit_posts):
            location = post.get("location")
            if location and location.get("lat") is not None and location.get("lng") is not None:
                lat, lng = location["lat"], location["lng"]
                key = f"{lat}, {lng}"
                taken_at = post.get("taken_at")
                prev = locations.get(key)
                if prev is None or (taken_at or 0) > (prev[1] or 0):
                    locations[key] = (lat, lng, taken_at, location.get("name"))
        addresses = []
        failed = 0
        for coords, (lat, lng, taken_at, name) in locations.items():
            details = reverse(coords)
            if details is None:
                failed += 1
                continue
            when = datetime.fromtimestamp(taken_at).strftime("%Y-%m-%d %H:%M:%S") if taken_at else None
            addresses.append({"address": details.address, "name": name, "lat": lat, "lng": lng, "time": when})
        if locations and failed == len(locations):
            raise BackendError(
                f"reverse geocoding failed for all {failed} location(s) - the geocoding "
                "service (Nominatim) may be unreachable (check network/SSL configuration)")
        return sorted(addresses, key=lambda a: a["time"] or "", reverse=True)

    def get_photo_descriptions(self, limit_posts: Optional[int] = None) -> dict:
        """Alt text of the target's photos (only ever filled when the author
        wrote it themselves)."""
        self._require_public()
        photos = [
            (media, post)
            for post in self._get_feed(limit=limit_posts)
            for media in post.get("carousel_media") or [post]
            if media.get("media_type") == 1
        ]
        descriptions = [
            {"description": media["accessibility_caption"],
             "taken_at": media.get("taken_at") or post.get("taken_at"),
             **self._media_files(media)}
            for media, post in photos
            if media.get("accessibility_caption")
        ]
        return {"photos_scanned": len(photos), "with_alt_text": len(descriptions), "descriptions": descriptions}

    def get_comment_data(self, limit_posts: Optional[int] = None) -> list:
        self._require_public()
        comments = []
        for post in self._get_feed(limit=limit_posts):
            post_id = post.get("id")
            for comment in self._get_comments(post_id):
                comments.append({
                    "post_id": post_id,
                    "user_id": comment.get("user_id"),
                    "username": (comment.get("user") or {}).get("username"),
                    "comment": comment.get("text"),
                })
        return comments

    def get_people_who_commented(self, limit_posts: Optional[int] = None) -> list:
        self._require_public()
        users: dict = {}
        for post in self._get_feed(limit=limit_posts):
            for comment in self._get_comments(post.get("id")):
                user = comment.get("user") or {}
                pk = user.get("pk")
                if pk is None:
                    continue
                if pk in users:
                    users[pk]["counter"] += 1
                else:
                    users[pk] = {"id": pk, "username": user.get("username"),
                                 "full_name": user.get("full_name"), "counter": 1}
        return sorted(users.values(), key=lambda u: u["counter"], reverse=True)

    def get_people_who_tagged(self, limit: Optional[int] = None) -> list:
        self._require_public()
        posts = self._paginate(self.api.user_tag_medias_v2, "items", limit=limit)
        users: dict = {}
        for post in posts:
            tag = post.get("user") or {}
            pk = tag.get("pk")
            if pk is None:
                continue
            if pk in users:
                users[pk]["counter"] += 1
            else:
                users[pk] = {"id": pk, "username": tag.get("username"),
                             "full_name": tag.get("full_name"), "counter": 1}
        return sorted(users.values(), key=lambda u: u["counter"], reverse=True)

    def get_people_tagged_by_user(self, limit_posts: Optional[int] = None) -> list:
        self._require_target()
        tagged: dict = {}
        for post in self._get_feed(limit=limit_posts):
            usertags = post.get("usertags") or []
            if isinstance(usertags, dict):
                usertags = usertags.get("in") or []
            for tag in usertags:
                user = tag.get("user") or {}
                pk = user.get("pk")
                if pk is None:
                    continue
                if pk in tagged:
                    tagged[pk]["posts"] += 1
                else:
                    tagged[pk] = {"id": pk, "username": user.get("username"),
                                  "full_name": user.get("full_name"), "posts": 1}
        return sorted(tagged.values(), key=lambda u: u["posts"], reverse=True)

    def get_user_photos(self, limit: Optional[int] = None) -> list:
        self._require_public()
        photos = []
        for item in self._iter_items(self.api.user_medias_g2, "items", cursor_param="next_page_id"):
            candidates = (item.get("carousel_media") or []) if item.get("media_type") == 8 else [item]
            for media in candidates:
                if media.get("media_type") != 1:
                    continue
                if limit is not None and len(photos) >= limit:
                    return photos
                versions = (media.get("image_versions2") or {}).get("candidates", [])
                if versions:
                    photos.append({"id": media.get("id"), "url": versions[0]["url"]})
        return photos

    def get_user_stories(self) -> list:
        self._require_public()
        try:
            data = self.api.user_stories_v2(self.target_id)
        except RequestLimitReachedError:
            return []
        reel = data.get("reel")
        if not reel:
            return []
        stories = []
        for item in reel.get("items", []):
            image_candidates = (item.get("image_versions2") or {}).get("candidates") or []
            image_url = image_candidates[0].get("url") if image_candidates else None
            if item.get("media_type") == 1 and image_url:
                story = {"id": item.get("id"), "media_type": "photo", "url": image_url}
            elif item.get("media_type") == 2 and item.get("video_versions"):
                story = {"id": item.get("id"), "media_type": "video", "url": item["video_versions"][0]["url"]}
                if image_url:
                    story["thumbnail_url"] = image_url
            else:
                continue
            stories.append(story)
        return stories

    def get_highlights(self) -> list:
        """Story Highlights pinned on the profile (title, story count, cover)."""
        self._require_public()
        try:
            data = self.api.user_highlights_v2(self.target_id)
        except RequestLimitReachedError:
            return []
        tray = data.get("response", {}).get("tray") or []
        highlights = []
        for item in tray:
            cover = item.get("cover_media") or {}
            image = cover.get("cropped_image_version") or cover.get("full_image_version") or {}
            highlights.append({
                "id": (item.get("id") or "").split(":")[-1] or None,
                "title": item.get("title"),
                "media_count": item.get("media_count"),
                "thumbnail_url": image.get("url"),
                "created_at": item.get("created_at"),
                "last_updated": item.get("latest_reel_media"),
            })
        return highlights

    # ---- post by URL / shortcode / id (needs no target) ----

    _RAW_MEDIA_ID = re.compile(r"^\d+(_\d+)?$")
    _POST_PATH = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{6,})")
    _BARE_SHORTCODE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
    _SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    @classmethod
    def _shortcode_from(cls, value: str) -> Optional[str]:
        value = value.split("?", 1)[0].split("#", 1)[0]
        if "/share/" in value:
            return None
        found = cls._POST_PATH.search(value)
        if found:
            return found.group(1)
        if value.startswith(("http://", "https://")):
            return None
        code = value.strip("/").rsplit("/", 1)[-1]
        return code if cls._BARE_SHORTCODE.match(code) else None

    @classmethod
    def _pk_from_shortcode(cls, code: str) -> str:
        pk = 0
        for char in code[:11]:
            pk = pk * 64 + cls._SHORTCODE_ALPHABET.index(char)
        return str(pk)

    def _resolve_media_id(self, post: str) -> str:
        value = (post or "").strip()
        if not value:
            raise OsintServiceError("Specify a post/reel URL, shortcode or id.")
        if self._RAW_MEDIA_ID.match(value):
            return value
        code = self._shortcode_from(value)
        if code:
            return self._pk_from_shortcode(code)
        if not value.startswith(("http://", "https://")):
            raise OsintServiceError(
                f"Could not recognise {post!r} as a post. Use the post or reel URL, "
                "its shortcode, or its numeric id.")
        try:
            resolved = self.api.media_pk_from_url_v1(value)
        except RequestLimitReachedError:
            raise
        except Exception as e:
            raise OsintServiceError(f"Could not resolve {post!r} to a post: {e}.") from e
        resolved = normalize_payload(resolved)
        pk = str(resolved.get("pk") if isinstance(resolved, dict) else resolved).strip()
        if not pk or not pk.isdigit():
            raise OsintServiceError(f"Could not resolve {post!r} to a post.")
        return pk

    def get_post_comments(self, post: str, limit: Optional[int] = None) -> list:
        """Every comment on one specific post/reel - given its URL, shortcode
        or id. Needs no target account."""
        media_id = self._resolve_media_id(post)
        return [
            {"post_id": media_id, "user_id": comment.get("user_id"),
             "username": (comment.get("user") or {}).get("username"), "comment": comment.get("text")}
            for comment in self._get_comments(media_id, limit=limit)
        ]

    # ---- contacts (one lookup per candidate - expensive) ----

    def _contact_info(self, api_method, from_key: str, to_key: str,
                      match_limit: Optional[int] = 20, max_checks: Optional[int] = 100) -> list:
        self._require_public()
        candidates = self._paginate(api_method, "users", limit=max_checks)
        results = []
        checked = 0
        for user in candidates:
            if (match_limit is not None and len(results) >= match_limit) or (
                max_checks is not None and checked >= max_checks
            ):
                break
            checked += 1
            try:
                detail = self.api.user_by_id_v2(user["pk"])
            except RequestLimitReachedError:
                break
            value = (detail.get("user") or {}).get(from_key)
            if value:
                results.append({"id": user["pk"], "username": user.get("username"),
                                "full_name": user.get("full_name"), to_key: value})
        return results

    def get_followers_email(self, match_limit=20, max_checks=100) -> list:
        return self._contact_info(self.api.user_followers_g2, "public_email", "email", match_limit, max_checks)

    def get_followings_email(self, match_limit=20, max_checks=100) -> list:
        return self._contact_info(self.api.user_following_g2, "public_email", "email", match_limit, max_checks)

    def get_followers_phone(self, match_limit=20, max_checks=100) -> list:
        return self._contact_info(
            self.api.user_followers_g2, "contact_phone_number", "contact_phone_number", match_limit, max_checks)

    def get_followings_phone(self, match_limit=20, max_checks=100) -> list:
        return self._contact_info(
            self.api.user_following_g2, "contact_phone_number", "contact_phone_number", match_limit, max_checks)

    # ---- searches that belong to no account ----

    @staticmethod
    def _v1_media_to_legacy(media: dict) -> dict:
        out = dict(media)
        if media.get("thumbnail_url") and not media.get("image_versions2"):
            out["image_versions2"] = {"candidates": [{"url": media["thumbnail_url"]}]}
        if media.get("video_url") and not media.get("video_versions"):
            out["video_versions"] = [{"url": media["video_url"]}]
        if media.get("caption_text") and not media.get("caption"):
            out["caption"] = {"text": media["caption_text"]}
        if isinstance(media.get("taken_at"), str) or media.get("taken_at_ts"):
            out["taken_at"] = media.get("taken_at_ts")
        if media.get("resources"):
            out["carousel_media"] = [InstagramService._v1_media_to_legacy(r) for r in media["resources"]]
        return out

    @staticmethod
    def _authors(posts: list) -> list:
        counts: dict = {}
        for post in posts:
            user = post.get("user") or {}
            username = user.get("username")
            if not username:
                continue
            entry = counts.setdefault(username, {"username": username,
                                                 "full_name": user.get("full_name"), "posts": 0})
            entry["posts"] += 1
        return sorted(counts.values(), key=lambda u: u["posts"], reverse=True)

    def search_hashtag(self, hashtag: str, sort: str = "top", limit: Optional[int] = 30) -> dict:
        """Posts published with a hashtag (sort="top" or "recent"). No target."""
        name = (hashtag or "").strip().lstrip("#")
        if not name:
            raise OsintServiceError("Specify a hashtag to search for.")
        if sort not in ("top", "recent"):
            raise OsintServiceError(f"invalid sort: {sort!r} (use top or recent)")
        method = self.api.hashtag_medias_top_v2 if sort == "top" else self.api.hashtag_medias_recent_v2
        posts = []
        page_id = None
        while limit is None or len(posts) < limit:
            try:
                result = method(name, **({"page_id": page_id} if page_id else {}))
            except RequestLimitReachedError:
                break
            response = normalize_payload(result.get("response") or {})
            for section in response.get("sections") or []:
                for entry in (section.get("layout_content") or {}).get("medias") or []:
                    if entry.get("media"):
                        posts.append(entry["media"])
            posts.extend(self._v1_media_to_legacy(m) for m in response.get("items") or [])
            page_id = result.get("next_page_id")
            if not page_id:
                break
        posts = posts[:limit] if limit is not None else posts
        return {"hashtag": f"#{name}", "sort": sort, "posts": len(posts),
                "authors": self._authors(posts), "previews": [self._post_preview(p) for p in posts]}

    def search_location(self, place: str, limit: Optional[int] = 30) -> dict:
        """Recent posts published from a place (resolved by name first). No target."""
        query = (place or "").strip()
        if not query:
            raise OsintServiceError("Specify a place to search for.")
        try:
            found = normalize_payload(self.api.fbsearch_places_v2(query))
        except RequestLimitReachedError:
            return {"place": None, "posts": 0, "authors": [], "previews": [], "alternatives": []}
        places = [item.get("location") or {} for item in found.get("items") or []]
        places = [p for p in places if p.get("pk")]
        if not places:
            raise OsintServiceError(f"No place found for {query!r}.")
        matched = places[0]
        posts = []
        max_id = None
        while limit is None or len(posts) < limit:
            try:
                chunk = self.api.location_medias_recent_chunk_v1(
                    int(matched["pk"]), **({"max_id": max_id} if max_id else {}))
            except RequestLimitReachedError:
                break
            medias, max_id = (chunk + [None, None])[:2] if isinstance(chunk, list) else ([], None)
            posts.extend(self._v1_media_to_legacy(m) for m in normalize_payload(medias or []))
            if not max_id or not medias:
                break
        posts = posts[:limit] if limit is not None else posts
        return {
            "place": {"name": matched.get("name"), "address": matched.get("address"),
                      "city": matched.get("city"), "lat": matched.get("lat"), "lng": matched.get("lng")},
            "alternatives": [p.get("name") for p in places[1:6]],
            "posts": len(posts), "authors": self._authors(posts),
            "previews": [self._post_preview(p) for p in posts],
        }
