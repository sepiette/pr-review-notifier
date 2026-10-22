"""Collapsible sections: config starting state, remembered toggles, and the folded-section hint."""

from prn import store
from prn.config import parse, replies_section
from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node, test_config_text


def section(name="Team Review Requested", collapsed=False, prs=()):
    return store.InboxSection(name, True, True, list(prs), collapsed)


def test_config_sets_the_starting_state():
    assert not store.is_collapsed({}, section(collapsed=False))
    assert store.is_collapsed({}, section(collapsed=True))


def test_toggle_is_remembered():
    s = section(collapsed=False)
    assert store.toggle_collapsed(s) is True
    assert store.is_collapsed(store.read_collapsed(), s)
    assert store.toggle_collapsed(s) is False
    assert not store.is_collapsed(store.read_collapsed(), s)


def test_changing_the_config_overrides_an_old_toggle():
    store.set_collapsed(section(collapsed=False), True)            # I folded it while config said open
    assert store.is_collapsed(store.read_collapsed(), section(collapsed=False))
    # later I set `collapsed: false` -> `true` in config: config wins, my old toggle no longer applies
    assert store.is_collapsed(store.read_collapsed(), section(collapsed=True))
    store.set_collapsed(section(collapsed=True), False)            # and I can toggle again from there
    assert not store.is_collapsed(store.read_collapsed(), section(collapsed=True))


def test_collapsed_flows_from_config_to_inbox():
    text = test_config_text().replace("  - name: My Draft PRs\n", "  - name: My Draft PRs\n    collapsed: true\n")
    cfg = parse(text)
    inbox = store.write_inbox(evaluate([parse_pr(node(author=ME, draft=True))], cfg, ME), ME, next_poll_at=None)
    by_name = {s.name: s for s in store.read_inbox().sections}
    assert by_name["My Draft PRs"].collapsed and not by_name["My Open PRs"].collapsed
    assert inbox.total == 1


def test_built_in_replies_section_starts_folded():
    assert replies_section().collapsed is True


def test_folded_section_hint():
    plain = parse_pr(node(number=1))
    replied = parse_pr(node(number=2))
    replied.reply_count, replied.latest_reply_at = 1, "2026-10-22T12:00:00Z"
    s = section("Replies", prs=[plain, replied])
    assert store.section_hint(s, {}, ME, read_tracking=True, reply_tracking=True) == " · 💬 1 · 2 new"
    assert store.section_hint(s, {}, ME, read_tracking=False, reply_tracking=True) == " · 💬 1"
    assert store.section_hint(s, {}, ME, read_tracking=False, reply_tracking=False) == ""
