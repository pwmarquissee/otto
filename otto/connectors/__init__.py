"""Direct connectors: Otto reading a source itself instead of through a Claude Code
session.

The refresh path used to be a headless `claude -p` per domain: a whole session with
its MCP connectors loaded, costing a session's floor every four hours, and testable
only with Claude Code installed. A connector here does the two halves separately:
a plain HTTP read of the source (`google.py`, OAuth read-only scopes, bounded
windows) and one small model call for the judgement the old prompt made
(`classify.py`, "reply or awareness, and why"). Both halves are injectable, so the
whole refresh runs in a test with no network and no credentials.

Nothing switches on its own. A domain uses a connector only when its
`OTTO_REFRESH_<DOMAIN>_CONNECTOR` setting names one; otherwise `otto/refresh.py`
spawns the Claude Code session exactly as before.
"""
