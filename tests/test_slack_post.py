"""Top-level channel posts as Otto, and the freshness marker they stamp.

The bug these cover: /daily's summary is a top-level post, the only thing that could
make one was the Slack connector (OAuth'd as the owner, gated unattended), and the run
stamped success either way. Between 2026-08-12 and 2026-08-20 the summary did not
post for eight days and every health surface read green.

Nothing here reaches Slack. `send` is stubbed by attribute, AND OTTO_NO_SEND=1 is set
so a stub that stops binding refuses at the transport instead of posting into the
owner's ops feed (see slack.NO_SEND_ENV for the day that lesson was learned).
"""

from __future__ import annotations

import pytest

from otto import config, slack


OPS_FEED = "C0EXAMPLEOPS"


@pytest.fixture
def feed(monkeypatch):
    """The owner's ops feed, configured. It is config with no default, so it is set
    here to a known value rather than read from whatever the environment holds."""
    monkeypatch.setattr(config, "SLACK_CHANNEL_ID", OPS_FEED)
    monkeypatch.setattr(config, "POST_CHANNELS", {OPS_FEED})


@pytest.fixture
def posted(feed, monkeypatch):
    monkeypatch.setenv("OTTO_NO_SEND", "1")
    calls: list[dict] = []

    def fake_send(text, *, channel=None, emails=None, thread_ts=None, timeout=120):
        calls.append({"text": text, "channel": channel, "thread_ts": thread_ts})
        return (channel or "C0FAKE", "1700000000.000100")

    monkeypatch.setattr(slack, "send", fake_send)
    return calls


def test_posts_top_level_with_no_thread(posted):
    channel, ts = slack.post_to_channel(config.SLACK_CHANNEL_ID, "daily run - clean")
    assert (channel, ts) == (config.SLACK_CHANNEL_ID, "1700000000.000100")
    # The point of the whole change: no thread_ts, so it starts its own message.
    assert posted[0]["thread_ts"] is None


def test_refuses_a_channel_outside_the_allowlist(posted):
    with pytest.raises(slack.SlackError) as e:
        slack.post_to_channel("C0NOTALLOWED", "hello everyone")
    assert "may not post top-level" in str(e.value)
    assert not posted, "refusal must happen before transmission"


def test_reply_channel_is_not_a_post_channel(posted, monkeypatch):
    """A channel Otto may ANSWER in is not one Otto may ANNOUNCE into.

    A help channel is in REPLY_CHANNELS so Otto can answer a summoned thread. That must
    not let Otto start a top-level message in front of the whole channel.
    """
    monkeypatch.setattr(config, "REPLY_CHANNELS", {"C0EXAMPLEQA"})
    monkeypatch.setattr(config, "POST_CHANNELS", {config.SLACK_CHANNEL_ID})
    with pytest.raises(slack.SlackError):
        slack.post_to_channel("C0EXAMPLEQA", "attention everyone")


def test_reply_still_requires_a_thread(posted):
    """The relaxation is a separate door, not a hole in the old one."""
    with pytest.raises(slack.SlackError) as e:
        slack.reply_in_thread(config.SLACK_CHANNEL_ID, "", "no thread here")
    assert "thread timestamp is required" in str(e.value)


def test_daily_summary_schedule_exists_and_alarms(feed):
    """The half that makes silence loud.

    A manual cadence (nothing launches it) with a max age (silence alarms). Both
    matter: without the cadence it would show up as due work, and without the max age
    it would be another thing that can stop forever without saying so.
    """
    from otto.runners import scheduled

    marker = {s.name: s for s in scheduled.default_schedules()}[config.FEED_POST_SCHEDULE]
    assert marker.cadence.kind == "manual"
    assert marker.max_age_hours == 26
    assert marker.enabled


def test_no_feed_channel_means_no_marker_and_no_posting(monkeypatch):
    """Unconfigured is the closed position: nothing to post to, so no schedule that
    would alarm about a summary never landing, and every top-level post refused."""
    from otto.runners import scheduled

    monkeypatch.setattr(config, "SLACK_CHANNEL_ID", "")
    monkeypatch.setattr(config, "POST_CHANNELS", set())
    assert config.FEED_POST_SCHEDULE not in {s.name for s in scheduled.default_schedules()}
    with pytest.raises(slack.SlackError, match="may not post top-level"):
        slack.post_to_channel(OPS_FEED, "hello")


def test_never_stamped_marker_goes_stale_after_its_limit(feed):
    from datetime import datetime, timedelta, timezone

    from otto.runners import scheduled

    marker = {s.name: s for s in scheduled.default_schedules()}[config.FEED_POST_SCHEDULE]
    created = datetime.fromisoformat(marker.created.replace("Z", "+00:00"))
    # Wednesday-to-Wednesday: weekend hours are excluded for work schedules, so the
    # window has to be one that contains no weekend or the assertion is a calendar
    # question rather than a staleness one.
    assert scheduled.staleness(marker, created + timedelta(hours=1)) is None
    marker.created = "2026-09-02T08:00:00Z"  # a Wednesday
    level, detail = scheduled.staleness(
        marker, datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc))
    assert level in ("warn", "crit")
    assert "never run" in detail
