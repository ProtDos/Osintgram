#!/usr/bin/env python3
"""Examples of using the `igscope` module.

Run the whole file:               python example.py natgeo
Run one example by number:        python example.py natgeo 3

Before running anything, create a session once (see igscope/README.md):

    python -c "import igscope; igscope.login('my_account', 'my_password')"
    # or, from a browser sessionid cookie (no password):
    python -c "import igscope; igscope.login_by_sessionid('<sessionid>')"
"""
import asyncio
import json
import sys

from igscope import InstagramClient


def show(title, obj):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# --- 1. Profile: everything about an account in one call -------------------
def example_profile(ig, target):
    data = ig.query(target, "profile")
    show(f"[1] profile of @{target}", data)


# --- 2. A single capability, with a parameter ------------------------------
def example_single_capability(ig, target):
    data = ig.query(target, "followers", limit=25)
    show(f"[2] first 25 followers of @{target}", data)


# --- 3. Content: feed-derived stats. Cap the scan so it stays cheap/fast ---
def example_content(ig, target):
    # limit_posts caps how many posts are scanned; max_calls caps total requests.
    data = ig.query(target, "content", limit_posts=12, max_calls=40)
    show(f"[3] content analysis of @{target} (last 12 posts)", data)


# --- 4. Network overlap between two accounts -------------------------------
def example_compare(ig, target):
    data = ig.query(target, "compare", other="instagram", scope="followings", limit=200)
    show(f"[4] @{target} vs @instagram (common followings)", data)


# --- 5. Contacts: public emails among followers (expensive - bounded) ------
def example_contacts(ig, target):
    data = ig.query(target, "followers_email", match_limit=5, max_checks=30)
    show(f"[5] up to 5 public emails among @{target}'s followers", data)


# --- 6. General search: no target needed -----------------------------------
def example_search(ig, _target):
    data = ig.query(None, "search", hashtag="sunset", place="Berlin", limit=10)
    show("[6] search: #sunset + posts from Berlin", data)


# --- 7. Synchronous STREAM: react to each capability as it lands -----------
def example_stream(ig, target):
    print(f"\n{'=' * 70}\n[7] streaming content of @{target}\n{'=' * 70}")
    for part in ig.stream(target, "content", limit_posts=12, max_calls=40):
        if part["capability"] == "_summary":
            print(f"  -- done, {part['api_calls']} backend requests")
        elif part["ok"]:
            print(f"  [ok]   {part['capability']}")
        else:
            print(f"  [fail] {part['capability']}: {part['error']}")


# --- 8. ASYNC stream: same, off the event loop -----------------------------
def example_async(ig, target):
    async def run():
        print(f"\n{'=' * 70}\n[8] async-streaming profile+network of @{target}\n{'=' * 70}")
        for request in ("profile", "network"):
            async for part in ig.astream(target, request, limit=25, max_calls=20):
                tag = "ok" if part["ok"] else "fail"
                print(f"  [{tag}] {request}/{part['capability']}")
    asyncio.run(run())


EXAMPLES = [
    example_profile,
    example_single_capability,
    example_content,
    example_compare,
    example_contacts,
    example_search,
    example_stream,
    example_async,
]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python example.py <target_username> [example_number]")
        print(f"\nRequest groups:     {InstagramClient.groups()}")
        print(f"Single capabilities: {InstagramClient.capabilities()}")
        sys.exit(1)

    target = sys.argv[1]
    ig = InstagramClient()
    print(f"Logged in as @{ig.logged_in_as}")

    if len(sys.argv) >= 3:
        EXAMPLES[int(sys.argv[2]) - 1](ig, target)
    else:
        for fn in EXAMPLES:
            try:
                fn(ig, target)
            except Exception as e:  # noqa: BLE001 - keep going through the demo
                print(f"  (example {fn.__name__} failed: {e})")


if __name__ == "__main__":
    main()
