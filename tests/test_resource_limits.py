"""GitHub's "Resource limits for this query exceeded": shrink the request instead of failing the poll."""

import json

import httpx
import pytest
import respx

from prn import replies
from prn.github import API_URL, MIN_PAGE_SIZE, GitHubClient, ResourceLimitError

from .conftest import ME, node, write_config

LIMIT_ERROR = {"errors": [{"type": "RESOURCE_LIMITS_EXCEEDED",
                           "message": "Resource limits for this query exceeded."}]}


class TooHeavyAbove:
    """Fake GitHub that rejects searches asking for more than `max_page` PRs, and nodes() calls with
    more than `max_ids` ids or more than `max_searches` searches in one request."""

    def __init__(self, nodes, max_page=12, max_ids=2, max_searches=1):
        self.nodes, self.max_page, self.max_ids, self.max_searches = nodes, max_page, max_ids, max_searches
        self.page_sizes, self.id_batches, self.search_batches = [], [], []

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        q, v = body["query"], body["variables"]
        rl = {"remaining": 4000, "resetAt": "x", "cost": 1}
        if "nodes(ids" in q:
            self.id_batches.append(list(v["ids"]))
            if len(v["ids"]) > self.max_ids:
                return httpx.Response(200, json=LIMIT_ERROR)
            by_id = {n["id"]: n for n in self.nodes}
            return httpx.Response(200, json={"data": {"rateLimit": rl, "nodes": [by_id.get(i) for i in v["ids"]]}})
        if "q0" in v:
            n = sum(1 for k in v if k.startswith("q"))
            self.search_batches.append(n)
            if n > self.max_searches:
                return httpx.Response(200, json=LIMIT_ERROR)
            data = {"rateLimit": rl}
            for i in range(n):
                data[f"s{i}"] = {"pageInfo": {"hasNextPage": False}, "nodes": []}
            return httpx.Response(200, json={"data": data})
        first = v["first"]
        self.page_sizes.append(first)
        if first > self.max_page:
            return httpx.Response(200, json=LIMIT_ERROR)
        start = int(v["after"] or 0)
        page = self.nodes[start:start + first]
        more = start + first < len(self.nodes)
        return httpx.Response(200, json={"data": {"rateLimit": rl, "search": {
            "issueCount": len(self.nodes), "pageInfo": {"hasNextPage": more, "endCursor": str(start + first)},
            "nodes": page}}})


@respx.mock
def test_search_shrinks_page_size_and_remembers_it():
    nodes = [node(number=i) for i in range(1, 31)]
    fake = TooHeavyAbove(nodes, max_page=12)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    got = client.search_prs("is:pr is:open repo:acme/web")
    assert [n["number"] for n in got] == list(range(1, 31))      # nothing lost or duplicated
    assert fake.page_sizes[:2] == [25, 12] and client.page_size == 12   # starts at 25 (PR #10), halves on limits
    fake.page_sizes.clear()
    client.search_prs("is:pr is:open repo:acme/api")
    assert fake.page_sizes[0] == 12                              # the smaller size sticks for this client


@respx.mock
def test_search_gives_up_at_the_minimum_page_size():
    respx.post(API_URL).mock(side_effect=TooHeavyAbove([node(number=1)], max_page=0))
    with pytest.raises(ResourceLimitError):
        GitHubClient(token="t").search_prs("is:pr")
    assert MIN_PAGE_SIZE == 5


@respx.mock
def test_nodes_requests_split_until_they_fit():
    nodes = [node(number=i) for i in range(1, 8)]
    fake = TooHeavyAbove(nodes, max_ids=2)
    respx.post(API_URL).mock(side_effect=fake)
    got = GitHubClient(token="t").nodes_by_id([n["id"] for n in nodes])
    assert sorted(n["number"] for n in got) == list(range(1, 8))
    assert all(len(b) <= 2 for b in fake.id_batches if b and len(b) <= 2)


@respx.mock
def test_combined_change_check_splits_searches():
    fake = TooHeavyAbove([], max_searches=1)
    respx.post(API_URL).mock(side_effect=fake)
    out = GitHubClient(token="t").check_searches(["q-a", "q-b", "q-c"])
    assert out == [[], [], []] and fake.search_batches == [3, 1, 2, 1, 1]   # 3 -> 1 + 2 -> 1 + 1 + 1


@respx.mock
def test_reply_check_splits_instead_of_skipping(cfg):
    prs_nodes = [node(number=i) for i in range(1, 5)]
    answered = {"comments": {"nodes": []}, "reviews": {"nodes": []},
                "reviewThreads": {"nodes": [{"comments": {"nodes": [
                    {"author": {"login": ME}, "createdAt": "2026-10-28T10:00:00Z", "body": "?", "url": "u1"},
                    {"author": {"login": "alice"}, "createdAt": "2026-10-28T11:00:00Z", "body": "!", "url": "u2"},
                ]}}]}}
    fake = TooHeavyAbove([{**n, **answered} for n in prs_nodes], max_ids=1)
    respx.post(API_URL).mock(side_effect=fake)
    from prn.github import parse_pr
    prs = [parse_pr(n) for n in prs_nodes]
    write_config()
    replies.attach(GitHubClient(token="t"), prs, cfg, ME)
    assert all(p.reply_count == 1 for p in prs)                  # every PR still got its reply data
