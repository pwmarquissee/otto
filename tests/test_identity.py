"""Agent identity: whose credential is it, and does the table still match reality.

The module's whole value is that it cannot flatter Otto. Two ways it could, and both
are asserted against here:

  * reporting `ok` for a row that was never assessed, which would let `unverified`
    quietly count as scoped;
  * going red on the standing "Otto runs as the owner" gap, which would make a green run
    impossible until the entire build lands and so guarantee nobody runs the check.

The third group is the detector itself: a secret appearing inline, or a new
integration inheriting the owner's account, has to produce a distinct gap row. That is the
only class of identity finding worth interrupting a morning for, so it is the only one
that may alarm.
"""

from __future__ import annotations

import json

import pytest

from otto import config, identity


@pytest.fixture
def registries(monkeypatch, tmp_path):
    """Point the module at throwaway MCP registries.

    Both paths are resolved at import, so the constants are patched rather than the
    environment. Returns a writer taking (code_servers, desktop_servers).
    """
    code = tmp_path / "claude.json"
    desktop = tmp_path / "desktop.json"
    monkeypatch.setattr(identity, "CODE_REGISTRY", code)
    monkeypatch.setattr(identity, "DESKTOP_REGISTRY", desktop)

    def write(code_servers: dict | None = None, desktop_servers: dict | None = None,
              projects: dict | None = None) -> None:
        code.write_text(json.dumps({"mcpServers": code_servers or {},
                                    "projects": projects or {}}), encoding="utf-8")
        desktop.write_text(json.dumps({"mcpServers": desktop_servers or {}}),
                           encoding="utf-8")

    return write


def _decl(monkeypatch, *principals: config.Principal, baseline: int | None = None) -> None:
    """Replace the declared table, and by default the baseline that matches it.

    Leaving the real `INHERITED_BASELINE` in place made three tests fail on a
    baseline-stale row they were not about, which is the same trap the module warns
    against: a baseline that does not describe the table it guards.
    """
    monkeypatch.setattr(config, "PRINCIPALS", tuple(principals))
    if baseline is None:
        baseline = sum(1 for p in principals if p.principal == config.PRINCIPAL_HUMAN)
    monkeypatch.setattr(config, "INHERITED_BASELINE", baseline)


def _p(name: str, principal: str = config.PRINCIPAL_SHARED,
       credential: str = config.CRED_STORE,
       surface: str = config.SURFACE_CODE_MCP) -> config.Principal:
    return config.Principal(name, surface, principal, credential, audit="test")


# ---- names only ---------------------------------------------------------------

def test_only_env_key_names_are_read(registries, monkeypatch):
    """A value must never reach a finding. A length is a hint, a hash is an oracle."""
    registries({"falcon": {"env": {"FALCON_CLIENT_SECRET": "sk-live-do-not-leak",
                                   "FALCON_BASE_URL": "https://api.us-2.example"}}})
    _decl(monkeypatch, _p("falcon", credential=config.CRED_INLINE))
    blob = json.dumps([f._asdict() for f in identity.check()]) + identity.render(True)
    assert "sk-live-do-not-leak" not in blob
    assert "FALCON_CLIENT_SECRET" in blob


def test_client_id_region_and_url_are_not_secrets(registries, monkeypatch):
    """Three false positives per server would bury the one key that matters."""
    registries({"ninja": {"env": {"NINJAONE_CLIENT_ID": "x", "NINJAONE_REGION": "us"}}})
    _decl(monkeypatch, _p("ninja", credential=config.CRED_STORE))
    assert [f.state for f in identity.check()] == ["ok"]


# ---- the states --------------------------------------------------------------

def test_declared_inline_and_inline_is_expected_not_an_alarm(registries, monkeypatch):
    registries({"falcon": {"env": {"FALCON_CLIENT_SECRET": "x"}}})
    _decl(monkeypatch, _p("falcon", credential=config.CRED_INLINE))
    f = identity.check()[0]
    assert f.state == "known-inline"
    assert f.ok, "the standing gap must not fail the check or nobody runs it"
    assert identity.gaps() == []


def test_secret_appearing_where_the_table_says_store_is_a_regression(registries, monkeypatch):
    registries({"okta": {"env": {"OKTA_PRIVATE_KEY": "x"}}})
    _decl(monkeypatch, _p("okta", credential=config.CRED_STORE))
    f = identity.check()[0]
    assert f.state == "inline-regression"
    assert not f.ok
    assert [r["kind"] for r in identity.gaps()] == ["identity-drift"]


def test_fixed_row_reports_stale_declaration(registries, monkeypatch):
    """Drift in the good direction still has to be noticed, or the table rots."""
    registries({"falcon": {"env": {"FALCON_CLIENT_ID": "x"}}})
    _decl(monkeypatch, _p("falcon", credential=config.CRED_INLINE))
    assert identity.check()[0].state == "declaration-stale"


def test_unverified_credential_never_reports_ok(registries, monkeypatch):
    """No inline secret is not evidence of a scoped principal.

    google-multi has no inline env keys and still authenticates as a human; a token
    file on disk holding the owner's OAuth grant is the same gap wearing a better hat.
    """
    registries({"google-multi": {}})
    _decl(monkeypatch, _p("google-multi", config.PRINCIPAL_HUMAN, config.CRED_UNVERIFIED))
    f = identity.check()[0]
    assert f.state == "unverifiable"
    assert "not inspected" in f.detail


def test_external_and_account_rows_are_unverifiable_not_ok(registries, monkeypatch):
    registries({})
    _decl(monkeypatch,
          _p("aws", config.PRINCIPAL_HUMAN, config.CRED_SSO, config.SURFACE_EXTERNAL))
    assert identity.check()[0].state == "unverifiable"


def test_declared_but_unregistered_defers_to_the_manifest(registries, monkeypatch):
    """`otto manifest` owns the missing-capability alarm. Double-filing it is noise."""
    registries({})
    _decl(monkeypatch, _p("gone", credential=config.CRED_INLINE))
    assert identity.check()[0].state == "not-registered"
    assert identity.gaps() == []


# ---- both registries ---------------------------------------------------------

def test_desktop_registry_is_searched_too(registries, monkeypatch):
    """`arbiter` was found on the Desktop registry only, on this module's first run.

    Code and Desktop are separate registries and the Okta entry is the standing proof
    they drift; a checker that reads only Code is blind to half the credential surface.
    """
    registries(code_servers={}, desktop_servers={"arbiter": {"env": {"ARBITER_API_KEY": "x"}}})
    _decl(monkeypatch, _p("something-else", credential=config.CRED_STORE))
    assert identity.undeclared_inline() == [("arbiter", ["ARBITER_API_KEY"])]


def test_project_scoped_servers_are_unioned_not_skipped(registries, monkeypatch):
    """The bug `manifest._registry_servers` already made once: returning early on the
    global dict made every project-scoped server invisible."""
    registries(code_servers={"a": {}},
               projects={"C:/x": {"mcpServers": {"b": {"env": {"B_TOKEN": "x"}}}}})
    _decl(monkeypatch, _p("a"))
    assert identity.undeclared_inline() == [("b", ["B_TOKEN"])]


def test_undeclared_inline_credential_alarms(registries, monkeypatch):
    registries({"mystery": {"env": {"MYSTERY_TOKEN": "x"}}})
    _decl(monkeypatch, _p("known"))
    rows = identity.gaps()
    assert any(r["id"] == "gap:identity-undeclared:mystery" for r in rows)


def test_unreadable_registry_is_not_fatal(registries, monkeypatch, tmp_path):
    identity.CODE_REGISTRY.write_text("{not json", encoding="utf-8")
    identity.DESKTOP_REGISTRY.write_text("{}", encoding="utf-8")
    _decl(monkeypatch, _p("falcon", credential=config.CRED_INLINE))
    assert identity.check()[0].state == "not-registered"


# ---- the scoreboard and the regression alarm ---------------------------------

def test_scoreboard_counts_attribution(registries, monkeypatch):
    registries({})
    _decl(monkeypatch,
          _p("a", config.PRINCIPAL_AGENT, config.CRED_STORE, config.SURFACE_EXTERNAL),
          _p("b", config.PRINCIPAL_HUMAN, config.CRED_SSO, config.SURFACE_EXTERNAL),
          _p("c", config.PRINCIPAL_SHARED, config.CRED_INLINE, config.SURFACE_EXTERNAL),
          baseline=1)
    board = identity.scoreboard()
    assert board == {"total": 3, "attributable": 1, "inherited": 1, "baseline": 1,
                     "counts": {config.PRINCIPAL_AGENT: 1, config.PRINCIPAL_SHARED: 1,
                                config.PRINCIPAL_HUMAN: 1, config.PRINCIPAL_UNVERIFIED: 0},
                     "inline_credentials": 1}
    assert identity.gaps() == [], "at baseline, the standing gap must stay silent"


def test_new_inheritance_above_baseline_alarms(registries, monkeypatch):
    """The one thing worth waking up for: a new integration wired to the owner's account."""
    registries({})
    _decl(monkeypatch,
          _p("a", config.PRINCIPAL_HUMAN, config.CRED_SSO, config.SURFACE_EXTERNAL),
          _p("b", config.PRINCIPAL_HUMAN, config.CRED_SSO, config.SURFACE_EXTERNAL),
          baseline=1)
    rows = identity.gaps()
    assert [r["id"] for r in rows] == ["gap:identity-new-inheritance"]
    assert rows[0]["score"] >= 70


def test_progress_below_baseline_asks_for_the_baseline_to_be_lowered(registries, monkeypatch):
    """A baseline nobody maintains silences the detector exactly when it starts working."""
    registries({})
    _decl(monkeypatch,
          _p("a", config.PRINCIPAL_AGENT, config.CRED_STORE, config.SURFACE_EXTERNAL),
          baseline=2)
    assert [r["id"] for r in identity.gaps()] == ["gap:identity-baseline-stale"]


def test_render_names_the_zero_case_plainly(registries, monkeypatch):
    """Otto having no principal of its own is the headline, not a footnote."""
    registries({})
    _decl(monkeypatch,
          _p("a", config.PRINCIPAL_HUMAN, config.CRED_SSO, config.SURFACE_EXTERNAL),
          baseline=1)
    out = identity.render()
    assert "0 of 1" in out
    assert "no principal of its own" in out
