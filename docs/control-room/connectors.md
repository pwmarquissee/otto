# Direct connectors

A refresh used to be a headless Claude Code session per domain: the session loaded its
MCP connectors, read the calendar and the inbox, and wrote JSON back, at a session's
floor cost every four hours, and nothing about it could be tested without Claude Code
installed. A domain can now be switched to a direct connector instead: `otto` reads
Gmail and Calendar itself under its own read-only OAuth grant, makes one small model
call for the reply-or-awareness verdict, and writes the same snapshots the session
wrote. The session path stays the default and is untouched until a domain is switched.

Cost: one Claude Haiku 5.5 call on a page of subjects and snippets, on the order of a
tenth of a cent, against a whole Claude Code session with its system context and
connector definitions loaded. The calendar half needs no model at all.

## What the owner does once

### 1. An OAuth client in the Google Cloud Console

One client serves both aliases.

1. Open [console.cloud.google.com](https://console.cloud.google.com), pick or create
   a project (any name; it holds nothing but this client).
2. **APIs & Services > Library**: enable the **Gmail API** and the **Google Calendar
   API**.
3. **APIs & Services > OAuth consent screen**:
   - For a Workspace account (the `work` alias), choose **Internal**. Only accounts in
     that Workspace can consent, and the app needs no verification.
   - For a consumer account (the `personal` alias), the app must be **External** and
     can stay in **Testing**; add the owner's address under **Test users**. A testing
     app's refresh tokens expire after seven days unless the app is published, so for
     a personal account either publish it (it is private to the owner anyway) or
     expect `otto google auth personal` once a week.
   - Scopes: `.../auth/gmail.readonly` and `.../auth/calendar.readonly`, nothing else.
     The scope is the real control: a read-only grant cannot send, label or delete no
     matter what the code does.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**, type
   **Desktop app**. Download the JSON and put it somewhere outside the repo, for
   example `~/.claude/otto/google-client.json`.

### 2. Settings, in `otto.env`

```
OTTO_GOOGLE_CLIENT_SECRET_FILE=C:\Users\you\.claude\otto\google-client.json
OTTO_REFRESH_WORK_CONNECTOR=google:work
OTTO_REFRESH_PERSONAL_CONNECTOR=google:personal
OTTO_ANTHROPIC_API_KEY=sk-ant-...        # or ANTHROPIC_API_KEY in the environment
```

Optional: `OTTO_GOOGLE_MAIL_QUERY` (the Gmail search; the default is the last day
minus promotions, social, updates and forums), `OTTO_GOOGLE_MAIL_MAX` (threads read,
default 25), `OTTO_CLASSIFY_MODEL` (default `claude-haiku-5-5`).

Switching a domain on is a settings write that applies live; the next refresh for
that domain takes the direct path.

### 3. The consent flow, once per alias

```
otto google auth work
otto google auth personal
otto google status
```

`auth` opens the consent page in the browser, catches the redirect on a loopback
port, exchanges the code (PKCE), and writes `<OTTO_HOME>/google/<alias>.json`. That
file holds the refresh token for that mailbox. It is outside the repo and never
printed; losing it costs one more `auth`, leaking it gives the holder read access to
that mailbox until the grant is revoked at myaccount.google.com/permissions.

## How a direct refresh runs

`refresh.start(store, domain)` sees the domain's connector, checks the token file
exists (a missing or revoked grant comes back as the "skipped" reason and one notice
naming the command to run), records a Run with `runner="inline"` and no pid, and
does the work on a thread: today's events from the primary calendar (role and other
attendees worked out from the event), the inbox query's newest threads with their
From/To/Subject/Date headers and snippet (bodies never cross the wire), one
`messages.parse` call with a JSON schema for the verdicts, then the `agenda` and
`mail` snapshots in the shapes the Today view and meeting prep already read, the
owning schedule stamped, the Run closed with the call's token usage and cost. A
failure closes the Run as failed, stamps the schedule failed, and posts one notice;
the tick never sees an exception.

Everything is injectable: the HTTP session, the model call. `tests/test_connectors.py`
drives the whole path with a fake and no credentials.

## What is not covered

- Only `google` is a connector kind. Meeting notes still come through the Notion
  session path.
- The Workspace `work` alias needs the owner to be able to create a project in their
  organization's Google Cloud, or an admin to do it for them.
- `OTTO_ANTHROPIC_API_KEY` is a regular API key. The admin key the Anthropic probe
  checks cannot call the Messages API.
