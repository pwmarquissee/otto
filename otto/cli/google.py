"""`otto google`: the owner's Google grants for the direct refresh connectors."""

from __future__ import annotations

import sys

from .. import config
from ..client import Client
from ..connectors import google as _google
from ._fmt import C_DIM, C_GRN, C_RED, C_YEL, _age, _c


def cmd_google(args, client: Client) -> int:
    if args.action == "status":
        return _status()
    return _auth(args)


def _status() -> int:
    rows = _google.status()
    print(f"  client secret  {config.GOOGLE_CLIENT_SECRET_FILE or _c('unset (OTTO_GOOGLE_CLIENT_SECRET_FILE)', C_YEL)}")
    for r in rows:
        if r["present"]:
            who = f" {r['email']}" if r.get("email") else ""
            fresh = _age(r.get("last_refreshed")) if r.get("last_refreshed") else "never"
            print(f"  {_c(r['alias'], C_GRN):<18}{who}  refreshed {fresh}  "
                  f"{_c(', '.join(s.rsplit('/', 1)[-1] for s in r['scopes']), C_DIM)}")
        else:
            print(f"  {_c(r['alias'], C_DIM):<18} no token: otto google auth {r['alias']}")
    using = [d for d in config.DOMAINS
             if (config.REFRESH_SOURCES.get(d) or {}).get("connector")]
    if using:
        print(_c(f"  domains on the direct path: {', '.join(using)}", C_DIM))
    else:
        print(_c("  no domain is switched to a connector yet (OTTO_REFRESH_<DOMAIN>_CONNECTOR)", C_DIM))
    return 0


def _auth(args) -> int:
    """Run the consent flow in this process: the browser opens, the redirect lands
    on a loopback port, the token file is written under OTTO_HOME/google."""
    try:
        print(f"  opening the consent page for `{args.alias}`; waiting on the browser...")
        info = _google.authorize(args.alias, port=args.port, timeout=args.timeout)
    except _google.GoogleError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    except ValueError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 2
    who = f" for {info['email']}" if info.get("email") else ""
    print(_c(f"  stored a read-only grant{who} at {_google.token_path(args.alias)}", C_GRN))
    print(_c(f"  switch the domain on with OTTO_REFRESH_{args.alias.upper()}_CONNECTOR=google:{args.alias} "
             "in otto.env, then otto refresh --domain " + args.alias, C_DIM))
    return 0


def add_google(sub) -> None:
    s = sub.add_parser("google", help="the read-only Google grants behind the direct refresh")
    gs = s.add_subparsers(dest="action", required=True)
    a = gs.add_parser("auth", help="run the consent flow for one account alias")
    a.add_argument("alias", choices=_google.ALIASES)
    a.add_argument("--port", type=int, default=0, help="loopback port (default: any free one)")
    a.add_argument("--timeout", type=int, default=300, help="seconds to wait for the browser")
    a.set_defaults(fn=cmd_google)
    st = gs.add_parser("status", help="which aliases hold a token, and which domains use one")
    st.set_defaults(fn=cmd_google)
    s.set_defaults(fn=cmd_google)


PARSERS = {
    "google": add_google,
}
