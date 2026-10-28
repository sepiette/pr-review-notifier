"""Replies to me: answers in review threads I'm part of, and comments that @mention me.

A reply is *unanswered* while I haven't posted again since:
  - thread reply:  someone else commented in a review thread after my latest comment in that thread
  - mention:       someone else @mentioned me after my latest activity anywhere on the PR
                   (a comment, a review, or a thread comment)

Review threads and comment bodies are too heavy to fetch on every search, so they come from a
separate `nodes(ids:)` query that only runs for PRs whose `updatedAt` changed since the last time
(any comment, review, or push changes it). Results are cached in ~/.local/state/prn/replies.json.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import paths
from .config import Config
from .models import PR

log = logging.getLogger(__name__)

CHUNK = 10   # PRs per request: ~650 nodes each, so a request stays well under GitHub's cost limits

REPLIES_QUERY = """
query($ids: [ID!]!) {
  rateLimit { remaining resetAt cost }
  nodes(ids: $ids) {
    ... on PullRequest {
      id
      comments(last: 30) { nodes { author { login } createdAt body url } }
      reviews(last: 20) { nodes { author { login } submittedAt body url } }
      reviewThreads(last: 40) {
        nodes { comments(last: 15) { nodes { author { login } createdAt body url } } }
      }
    }
  }
}
"""


@dataclass
class ReplyInfo:
    count: int = 0            # unanswered replies + mentions
    latest_at: str = ""       # ISO timestamp of the newest one
    latest_author: str = ""
    latest_kind: str = ""     # "reply" | "mention"
    latest_url: str = ""      # deep link to that comment

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _login(actor: Optional[dict]) -> str:
    return ((actor or {}).get("login") or "ghost").lower().removesuffix("[bot]")


def mention_pattern(me: str) -> re.Pattern:
    return re.compile(rf"(?<![\w/-])@{re.escape(me)}(?![\w-])", re.IGNORECASE)


def analyze(node: dict, me: str) -> ReplyInfo:
    """Find unanswered replies and mentions in one PR's comments, reviews, and review threads."""
    me = me.lower()
    mention = mention_pattern(me)
    issue_comments = [c for c in (node.get("comments") or {}).get("nodes") or [] if c]
    reviews = [r for r in (node.get("reviews") or {}).get("nodes") or [] if r]
    threads = [[c for c in ((t or {}).get("comments") or {}).get("nodes") or [] if c]
               for t in (node.get("reviewThreads") or {}).get("nodes") or []]

    def when(item: dict) -> str:
        return item.get("createdAt") or item.get("submittedAt") or ""

    everything = issue_comments + reviews + [c for t in threads for c in t]
    my_last = max((when(c) for c in everything if _login(c.get("author")) == me), default="")

    found: dict[str, tuple[str, str, str]] = {}   # url -> (when, author, kind)

    # 1. Replies in review threads I'm part of
    for thread in threads:
        mine = [when(c) for c in thread if _login(c.get("author")) == me]
        if not mine:
            continue
        last_mine = max(mine)
        for c in thread:
            who = _login(c.get("author"))
            if who != me and when(c) > last_mine:
                found[c.get("url") or f"{when(c)}:{who}"] = (when(c), who, "reply")

    # 2. @mentions after my latest activity on the PR
    for c in everything:
        who = _login(c.get("author"))
        key = c.get("url") or f"{when(c)}:{who}"
        if who != me and key not in found and when(c) > my_last and mention.search(c.get("body") or ""):
            found[key] = (when(c), who, "mention")

    if not found:
        return ReplyInfo()
    url, (at, who, kind) = max(found.items(), key=lambda kv: kv[1][0])
    return ReplyInfo(count=len(found), latest_at=at, latest_author=who, latest_kind=kind, latest_url=url)


# ---------------------------------------------------------------- cache + fetch


def _read_cache() -> dict:
    try:
        return json.loads(paths.replies_path().read_text(encoding="utf-8")).get("prs", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache(cache: dict) -> None:
    from .store import _atomic_write_json  # local import: store imports models, avoid cycles at import time
    _atomic_write_json(paths.replies_path(), {"prs": cache})


def attach(client, prs: list[PR], cfg: Config, me: str) -> None:
    """Fill in reply fields on `prs`, refetching threads only for PRs that changed. Never raises:
    a failure here just leaves the previous reply data in place."""
    if not cfg.settings.reply_tracking or not prs:
        return
    cache = _read_cache()
    stale = [p for p in prs if cache.get(p.id, {}).get("updated_at") != p.updated_at
             or cache.get(p.id, {}).get("me") != me.lower()]
    for i in range(0, len(stale), CHUNK):
        chunk = stale[i:i + CHUNK]
        try:
            if hasattr(client, "split_on_limits"):
                nodes = client.split_on_limits(REPLIES_QUERY, [p.id for p in chunk])   # halves on resource limits
            else:
                nodes = client.graphql(REPLIES_QUERY, {"ids": [p.id for p in chunk]}).get("nodes")
        except Exception as exc:  # keep polling even if this extra request fails
            log.warning("reply check failed for %d PRs: %s", len(chunk), exc)
            continue
        by_id = {n["id"]: n for n in nodes or [] if n}
        for p in chunk:
            if p.id in by_id:
                cache[p.id] = {"updated_at": p.updated_at, "me": me.lower(),
                               "info": analyze(by_id[p.id], me).to_dict()}
    live = {p.id for p in prs}
    cache = {k: v for k, v in cache.items() if k in live}
    _write_cache(cache)
    for p in prs:
        info = ReplyInfo(**cache[p.id]["info"]) if p.id in cache else ReplyInfo()
        p.reply_count, p.latest_reply_at = info.count, info.latest_at
        p.latest_reply_author, p.latest_reply_kind, p.latest_reply_url = (
            info.latest_author, info.latest_kind, info.latest_url)


def describe(pr: PR) -> str:
    """Short human text for the newest reply, e.g. '@alice replied to your comment'."""
    if not pr.reply_count:
        return ""
    who = f"@{pr.latest_reply_author}"
    text = f"{who} replied to your comment" if pr.latest_reply_kind == "reply" else f"{who} mentioned you"
    return text if pr.reply_count == 1 else f"{text} (+{pr.reply_count - 1} more)"
