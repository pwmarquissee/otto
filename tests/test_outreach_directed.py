"""outreach.directed: a colleague DM the owner asked for, sent now, as Otto.

Nothing here can reach Slack. The transport is stubbed by attribute on the module
outreach imports, AND OTTO_NO_SEND=1 is set, so if the stub ever stops binding the
real send refuses at the transport instead of messaging somebody (see slack.NO_SEND_ENV
for the day that lesson was learned).

Who counts as a colleague is config, not code: ORG_DOMAINS and CONTRACTOR_DOMAINS
are set explicitly here so the roster gate is exercised against known values and
not against whatever the environment happened to hold.
"""

from __future__ import annotations

import pytest

from otto import config, outreach

OWNER = "owner@example.com"
COLLEAGUE = {"slug": "alex", "login": "alex@example.com", "email": "alex@example.com",
             "display_name": "Alex Rivera", "external": False}
CONTRACTOR = {"slug": "sam", "login": "sam@ext.example.com", "email": "sam@ext.example.com",
              "display_name": "Sam Contractor", "external": False}
STRANGER = {"slug": "pat", "login": "pat@other-company.test", "email": "pat@other-company.test",
            "display_name": "Pat Elsewhere", "external": False}
EXT = {"slug": "vendor", "login": "v@vendor.test", "email": "v@vendor.test",
       "display_name": "Vendor Person", "external": True}
ROSTER = {"alex": COLLEAGUE, "sam": CONTRACTOR, "pat": STRANGER, "vendor": EXT}


@pytest.fixture
def org(monkeypatch):
    monkeypatch.setattr(config, "ORG_DOMAINS", ("example.com",))
    monkeypatch.setattr(config, "CONTRACTOR_DOMAINS", ("ext.example.com",))
    monkeypatch.setattr(config, "SLACK_OWNER_EMAIL", OWNER)
    monkeypatch.setattr(config, "OUTREACH_ALLOW_CONTRACTORS", False)


@pytest.fixture
def sent(org, monkeypatch):
    monkeypatch.setenv("OTTO_NO_SEND", "1")
    calls: list[dict] = []

    def fake_send(text, *, channel=None, emails=None, thread_ts=None, timeout=120):
        calls.append({"text": text, "emails": list(emails or []), "channel": channel})
        return ("G0FAKE", "1.0")

    monkeypatch.setattr(outreach.slack, "send", fake_send)
    monkeypatch.setattr(outreach.people, "get",
                        lambda who: ROSTER.get(who.split("@")[0].lower()))
    return calls


def test_directed_sends_now_as_group_dm_with_the_owner_first(store, sent):
    item = outreach.directed(store, to=["alex"], body="quick check on the login alert",
                             why="the owner asked", run_id="r1", task_id="t1")
    assert item.state == "sent"
    assert item.hold_minutes == 0
    # The owner's decision, not the timer's and not Otto's own.
    assert item.decided_by == "owner"
    assert item.recipient_kind == "member"
    assert item.source == "directed"
    assert item.run_id == "r1" and item.task_id == "t1"
    assert sent == [{"text": "quick check on the login alert",
                     "emails": [OWNER, "alex@example.com"],
                     "channel": None}]
    # In the same ledger as everything else Otto sends.
    assert [o.id for o in store.outreach()] == [item.id]


def test_directed_skips_the_forbidden_subject_filter(store, sent):
    # "investigation" and "incident" are refused for Otto's OWN initiative. The
    # owner asking for the message is the authorization, so they go through here.
    item = outreach.directed(store, to=["alex"],
                             body="security investigation follow-up, not an incident")
    assert item.state == "sent"


def test_directed_still_needs_the_roster(store, sent):
    with pytest.raises(outreach.Refused):
        outreach.directed(store, to=["nobody"], body="hi")
    with pytest.raises(outreach.Refused, match="external"):
        outreach.directed(store, to=["vendor"], body="hi")
    assert sent == []


def test_directed_refuses_a_login_outside_the_org_domains(store, sent):
    """A dossier is necessary, not sufficient: the login has to be on a domain the
    org owns, or a mistyped roster row could address anybody."""
    with pytest.raises(outreach.Refused, match="example.com"):
        outreach.directed(store, to=["pat"], body="hi")
    assert sent == []


def test_contractors_are_a_separate_switch(store, sent, monkeypatch):
    with pytest.raises(outreach.Refused, match="contractor"):
        outreach.directed(store, to=["sam"], body="hi")
    assert sent == []
    monkeypatch.setattr(config, "OUTREACH_ALLOW_CONTRACTORS", True)
    item = outreach.directed(store, to=["sam"], body="hi")
    assert item.state == "sent"
    assert item.recipient_kind == "contractor"


def test_directed_to_the_owner_alone_points_at_tell(store, sent):
    with pytest.raises(outreach.Refused, match="otto tell"):
        outreach.directed(store, to=[OWNER], body="hi")
    # The owner named alongside a colleague is fine and is not added twice.
    item = outreach.directed(store, to=[OWNER, "alex"], body="hi")
    assert item.state == "sent"
    assert sent[-1]["emails"].count(OWNER) == 1


def test_directed_empty_body_refused(store, sent):
    with pytest.raises(outreach.Refused, match="empty"):
        outreach.directed(store, to=["alex"], body="   ")
    assert sent == []


def test_directed_per_run_brake(store, sent, monkeypatch):
    monkeypatch.setattr(config, "DIRECTED_MAX_PER_RUN", 2)
    outreach.directed(store, to=["alex"], body="one", run_id="r1")
    outreach.directed(store, to=["alex"], body="two", run_id="r1")
    with pytest.raises(outreach.Refused, match="cap is 2 per run"):
        outreach.directed(store, to=["alex"], body="three", run_id="r1")
    # A different run is the owner asking again, not a loop.
    assert outreach.directed(store, to=["alex"], body="four", run_id="r2").state == "sent"
    assert len(sent) == 3


def test_directed_transport_failure_is_recorded_not_raised(store, org, monkeypatch):
    monkeypatch.setenv("OTTO_NO_SEND", "1")
    monkeypatch.setattr(outreach.people, "get", lambda who: COLLEAGUE)

    def fail(*a, **k):
        raise outreach.slack.SlackError("the secrets CLI could not check out the token")

    monkeypatch.setattr(outreach.slack, "send", fail)
    item = outreach.directed(store, to=["alex"], body="hi")
    assert item.state == "failed"
    assert "secrets CLI" in (item.error or "")


def test_directed_refuses_at_transport_if_stub_is_gone(store, org, monkeypatch):
    # The belt under the braces: with no stub, OTTO_NO_SEND stops the real send.
    monkeypatch.setenv("OTTO_NO_SEND", "1")
    monkeypatch.setattr(outreach.people, "get", lambda who: COLLEAGUE)
    item = outreach.directed(store, to=["alex"], body="hi")
    assert item.state == "failed"
    assert "OTTO_NO_SEND" in (item.error or "")
