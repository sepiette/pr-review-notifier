"""Replies to me: detection, caching, filter, notifications, and the 💬 marker."""

import asyncio
import time
from datetime import datetime, timezone

from prn import events as ev
from prn import menu_style, replies, store
from prn.config import parse
from prn.engine import evaluate
from prn.github import parse_pr, plan_queries

from .conftest import ME, node, test_config_text, write_config


def c(who, at, body="", url=None):
    return {"author": {"login": who}, "createdAt": at, "body": body, "url": url or f"https://x/{who}/{at}"}


def r(who, at, body=""):
    return {"author": {"login": who}, "submittedAt": at, "body": body, "url": f"https://x/review/{who}/{at}"}


def pr_node(threads=(), comments=(), reviews=(), pid="PR_1"):
    return {"id": pid, "comments": {"nodes": list(comments)}, "reviews": {"nodes": list(reviews)},
            "reviewThreads": {"nodes": [{"comments": {"nodes": list(t)}} for t in threads]}}


T1, T2, T3, T4 = "2026-10-19T10:00:00Z", "2026-10-19T11:00:00Z", "2026-10-19T12:00:00Z", "2026-10-19T13:00:00Z"


# ---------------------------------------------------------------- analysis


def test_reply_in_my_thread_until_i_answer():
    asked = [c(ME, T1, "Why this?"), c("alice", T2, "Because…", url="https://x/answer")]
    info = replies.analyze(pr_node(threads=[asked]), ME)
    assert (info.count, info.latest_author, info.latest_kind, info.latest_url) == (1, "alice", "reply", "https://x/answer")
    answered = asked + [c(ME, T3, "Thanks!")]
    assert replies.analyze(pr_node(threads=[answered]), ME).count == 0


def test_threads_im_not_in_are_ignored():
    assert replies.analyze(pr_node(threads=[[c("bob", T1, "?"), c("alice", T2, "!")]]), ME).count == 0


def test_my_thread_started_by_someone_else_counts():
    thread = [c("bob", T1, "Hmm"), c(ME, T2, "Can you explain?"), c("bob", T3, "Sure")]
    assert replies.analyze(pr_node(threads=[thread]), ME).count == 1


def test_mentions_after_my_last_activity():
    info = replies.analyze(pr_node(comments=[c(ME, T1, "pushed a fix"), c("alice", T2, f"thanks @{ME}!")]), ME)
    assert (info.count, info.latest_kind) == (1, "mention")
    # mention in a review body counts too
    assert replies.analyze(pr_node(reviews=[r("bob", T2, f"@{ME.upper()} please look")]), ME).count == 1
    # I've posted since the mention: answered
    answered = pr_node(comments=[c("alice", T1, f"@{ME} ?"), c(ME, T2, "yes")])
    assert replies.analyze(answered, ME).count == 0


def test_mention_matching_is_strict():
    for body in (f"mail {ME}@example.com", f"@{ME}-bot did it", f"see github.com/@{ME}x", "no mention"):
        assert replies.analyze(pr_node(comments=[c("alice", T2, body)]), ME).count == 0, body
    for body in (f"@{ME}", f"(@{ME})", f"cc @{ME}, thanks", f"@{ME}: yes"):
        assert replies.analyze(pr_node(comments=[c("alice", T2, body)]), ME).count == 1, body


def test_thread_reply_that_also_mentions_me_counts_once_and_bots_normalize():
    thread = [c(ME, T1, "?"), c("coderabbitai[bot]", T2, f"@{ME} good question", url="https://x/1")]
    info = replies.analyze(pr_node(threads=[thread]), ME)
    assert (info.count, info.latest_kind, info.latest_author) == (1, "reply", "coderabbitai")


def test_latest_of_several():
    t1 = [c(ME, T1), c("alice", T2)]
    t2 = [c(ME, T1), c("bob", T4, url="https://x/newest")]
    info = replies.analyze(pr_node(threads=[t1, t2]), ME)
    assert (info.count, info.latest_author, info.latest_url) == (2, "bob", "https://x/newest")
    pr = parse_pr(node())
    pr.reply_count, pr.latest_reply_author, pr.latest_reply_kind = 2, "bob", "reply"
    assert replies.describe(pr) == "@bob replied to your comment (+1 more)"


# ---------------------------------------------------------------- attach + cache


class FakeClient:
    def __init__(self, answer, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def graphql(self, query, variables):
        self.calls.append(list(variables["ids"]))
        if self.fail:
            raise RuntimeError("boom")
        return {"nodes": [self.answer(i) for i in variables["ids"]]}


def test_attach_caches_by_updated_at(cfg):
    thread = [c(ME, T1), c("alice", T2)]
    client = FakeClient(lambda i: pr_node(threads=[thread], pid=i))
    prs = [parse_pr(node(number=1, updated=T2)), parse_pr(node(number=2, updated=T2))]
    replies.attach(client, prs, cfg, ME)
    assert client.calls == [["PR_1", "PR_2"]] and prs[0].reply_count == 1 and prs[0].latest_reply_author == "alice"

    again = [parse_pr(node(number=1, updated=T2)), parse_pr(node(number=2, updated=T3))]
    replies.attach(client, again, cfg, ME)
    assert client.calls[-1] == ["PR_2"]                     # only the PR that changed
    assert again[0].reply_count == 1                        # PR_1 comes from the cache


def test_attach_never_breaks_polling(cfg):
    prs = [parse_pr(node(number=1))]
    replies.attach(FakeClient(None, fail=True), prs, cfg, ME)
    assert prs[0].reply_count == 0


def test_attach_is_off_when_reply_tracking_is_off(cfg):
    cfg.settings.reply_tracking = False
    client = FakeClient(lambda i: pr_node(pid=i))
    replies.attach(client, [parse_pr(node())], cfg, ME)
    assert client.calls == []


# ---------------------------------------------------------------- filter, search, section, badge


def with_reply(n, count=1, at=T2, who="alice", kind="reply"):
    pr = parse_pr(n)
    pr.reply_count, pr.latest_reply_at, pr.latest_reply_author, pr.latest_reply_kind = count, at, who, kind
    pr.latest_reply_url = "https://x/comment"
    return pr


def test_replies_mark_inbox_prs_without_a_section_of_their_own(cfg):
    """No "Replies to Me" section in the starter config: replies only mark PRs already in a section."""
    assert not any(s.filters.has_replies_to_me for s in cfg.sections)
    in_teammates = with_reply(node(author="alice", number=11))
    elsewhere = with_reply(node(author="dave", number=12))     # in no section: not shown at all
    res = {s.name: [p.number for p in s.prs] for s in evaluate([in_teammates, elsewhere], cfg, ME)}
    assert res["Teammates Need Review"] == [11]
    assert 12 not in sum(res.values(), [])


def test_optional_replies_section_still_works(cfg):
    cfg.sections.append(parse("defaults: {repos: [acme/web]}\n"
                              "sections: [{name: Replies, filters: {has_replies_to_me: true}}]").sections[0])
    res = {s.name: [p.number for p in s.prs] for s in evaluate([with_reply(node(author="dave", number=12))], cfg, ME)}
    assert res["Replies"] == [12]


def test_replies_never_count_toward_badge(cfg):
    mine = with_reply(node(author=ME, number=14))          # My Open PRs: no badge, with or without replies
    inbox = store.write_inbox(evaluate([mine], cfg, ME), ME, next_poll_at=None)
    assert inbox.total == 1 and inbox.badge_count == 0


def test_extra_searches_only_when_a_section_wants_replies(cfg):
    assert not any("involves:" in q or "reviewed-by:" in q for q in plan_queries(cfg, ME))   # starter config
    cfg.sections.append(parse("defaults: {repos: [acme/web]}\n"
                              "sections: [{name: Replies, filters: {has_replies_to_me: true}}]").sections[0])
    qs = plan_queries(cfg, ME)
    assert any(f"involves:{ME}" in q for q in qs) and any(f"reviewed-by:{ME}" in q for q in qs)


# ---------------------------------------------------------------- notifications


def snap(cfg, prs):
    return ev.build_snapshot(evaluate(prs, cfg, ME), prs, ME)


def test_new_reply_notifies_with_deep_link(cfg):
    cfg.settings.notifications.replies = True
    before = parse_pr(node(author="alice", number=20))
    after = with_reply(node(author="alice", number=20), at=T3)
    evs = ev.diff(snap(cfg, [before]), evaluate([after], cfg, ME), [after], cfg, ME)
    reply_evs = [e for e in evs if e.kind == ev.REPLY]
    assert [e.detail for e in reply_evs] == ["@alice replied to your comment"]
    note = ev.to_notifications(evs)[0]
    assert note.title == "@alice replied to your comment" and note.url == "https://x/comment"


def test_reply_notifications_are_off_by_default_and_baselined(cfg):
    after = with_reply(node(author="alice", number=21), at=T3)
    before = parse_pr(node(author="alice", number=21))
    assert not [e for e in ev.diff(snap(cfg, [before]), evaluate([after], cfg, ME), [after], cfg, ME)
                if e.kind == ev.REPLY]                      # default: replies: false
    cfg.settings.notifications.replies = True
    old = snap(cfg, [before])
    for entry in old.values():
        entry.pop("latest_reply_at")                         # snapshot from before this feature
    assert not [e for e in ev.diff(old, evaluate([after], cfg, ME), [after], cfg, ME) if e.kind == ev.REPLY]


def test_same_reply_doesnt_notify_twice(cfg):
    cfg.settings.notifications.replies = True
    pr = with_reply(node(author="alice", number=22), at=T3)
    assert not [e for e in ev.diff(snap(cfg, [pr]), evaluate([pr], cfg, ME), [pr], cfg, ME) if e.kind == ev.REPLY]


# ---------------------------------------------------------------- marker


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def test_marker_clears_when_opened_and_returns_on_new_reply():
    pr = with_reply(node(number=30), at=T2)
    assert store.has_unread_reply({}, pr)
    seen = {pr.key: {"at": epoch(T3)}}                       # opened after the reply
    assert not store.has_unread_reply(seen, pr)
    newer = with_reply(node(number=30), at=T4)
    assert store.has_unread_reply(seen, newer)
    assert store.has_unread_reply(seen, pr, read_tracking=False)   # no read tracking: until I answer
    assert not store.has_unread_reply({}, parse_pr(node(number=31)))


def test_menu_marker_is_never_muted():
    pr = with_reply(node(number=40, title="Q"), count=2)
    segs = menu_style.pr_segments(pr, 50, read=True, reply=True)
    assert segs[-1] == ("  💬 2", "normal")
    assert all(role.endswith("muted") for _, role in segs[:-1])
    assert "💬" not in menu_style.plain(menu_style.pr_segments(pr, 50))
    assert "💬" in menu_style.LEGEND


def test_tui_shows_marker_and_detail(cfg):
    from textual.widgets import Static

    from prn.tui import PRNTui

    write_config()
    pr = with_reply(node(author="alice", number=50, title="Question PR"), at=T2)
    store.write_inbox(evaluate([pr], cfg, ME), ME, next_poll_at=None)

    async def go():
        tui = PRNTui()
        async with tui.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            ol = tui.query_one("#list")
            rows = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            row = next(r for r in rows if "#50" in r)
            assert "💬" in row
            detail = str(tui.query_one("#detail", Static).render())
            assert "Replies to you" in detail and "@alice replied to your comment" in detail
            status = str(tui.query_one("#status", Static).render())
            assert "💬" not in status                          # not part of any count
    asyncio.run(go())


def test_replies_section_does_not_scan_every_open_pr(cfg):
    """Regression: a replies section has no author filter, but must not trigger a repo-wide search."""
    cfg.sections.append(parse("defaults: {repos: [acme/web]}\n"
                              "sections: [{name: Replies, filters: {has_replies_to_me: true}}]").sections[0])
    qs = plan_queries(cfg, ME)
    narrowing = ("author:", "review-requested:", "involves:", "reviewed-by:")
    assert all(any(n in q for n in narrowing) for q in qs), qs
    # a section that really has no narrowing still gets a repo scan
    cfg.sections.append(parse('defaults: {repos: [acme/web]}\nsections: [{name: Everything}]').sections[0])
    assert any("repo:acme/web" in q and "author:" not in q for q in plan_queries(cfg, ME))


# ---------------------------------------------------------------- settings.replies_section


def cfg_with(settings_yaml="", sections_yaml=""):
    """The test config with some `key: value` settings overridden (lines like '  replies_section: true')."""
    import re
    text = test_config_text()
    for line in filter(None, settings_yaml.splitlines()):
        key = line.split(":")[0].strip()
        text, n = re.subn(rf"^  {key}:.*$", line, text, count=1, flags=re.M)
        assert n == 1, key
    if sections_yaml:
        text = text.rstrip("\n") + "\n" + sections_yaml
    return parse(text)


def test_replies_section_is_off_by_default(cfg):
    assert cfg.settings.replies_section is False
    assert "Replies to Me" not in [s.name for s in cfg.sections]


def test_replies_section_setting_adds_it_last():
    cfg = cfg_with("  replies_section: true\n")
    last = cfg.sections[-1]
    assert (last.name, last.notify, last.badge, last.filters.has_replies_to_me) == ("Replies to Me", False, False, True)
    # PRs outside every other section land there, and it never adds to the badge
    elsewhere = with_reply(node(author="dave", number=60))
    results = evaluate([elsewhere], cfg, ME)
    assert {s.name: [p.number for p in s.prs] for s in results}["Replies to Me"] == [60]
    assert store.write_inbox(results, ME, next_poll_at=None).badge_count == 0
    # and it adds only the two narrow searches, never a scan of every open PR
    qs = plan_queries(cfg, ME)
    assert any(f"involves:{ME}" in q for q in qs) and any(f"reviewed-by:{ME}" in q for q in qs)
    assert all(any(n in q for n in ("author:", "review-requested:", "involves:", "reviewed-by:")) for q in qs)


def test_replies_section_needs_reply_tracking():
    cfg = cfg_with("  replies_section: true\n  reply_tracking: false\n")
    assert "Replies to Me" not in [s.name for s in cfg.sections]


def test_replies_section_doesnt_duplicate_a_custom_one():
    custom = "  - name: Answers\n    filters: {has_replies_to_me: true}\n"
    cfg = cfg_with("  replies_section: true\n", custom)
    assert [s.name for s in cfg.sections].count("Answers") == 1
    assert "Replies to Me" not in [s.name for s in cfg.sections]
    same_name = "  - name: Replies to Me\n    filters: {labels: [question]}\n"
    cfg = cfg_with("  replies_section: true\n", same_name)       # name taken: keep the user's section
    assert [s.name for s in cfg.sections].count("Replies to Me") == 1
