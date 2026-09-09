#!/usr/bin/env python3
"""One-time WHOOP OAuth bootstrap.

Opens the WHOOP consent screen, catches the redirect on localhost, exchanges
the authorization code, and prints the refresh token you paste into GitHub
Secrets. Run this once (and again only if the token is ever revoked).

    python3 scripts/whoop_auth.py

It prompts for the client id and secret without echoing them, and writes the
results to .env.local rather than printing them to the terminal.

Requires that the redirect URI below is registered on your app at
developer.whoop.com, exactly. Default is http://localhost:1111/callback;
set WHOOP_REDIRECT_URI to override.
"""
import getpass
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# WHOOP sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" signature with a 403 (error 1010, browser_signature_banned).
# Every request must carry a real product User-Agent.
UA = "whoop-dashboard/1.0"

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
# Must match a redirect URI registered on your app at developer.whoop.com,
# character for character. Override with WHOOP_REDIRECT_URI if you registered
# a different port.
REDIRECT_URI = os.environ.get(
    "WHOOP_REDIRECT_URI", "http://localhost:1111/callback"
)

# `offline` is what makes WHOOP issue a refresh token at all -- without it you
# get a lone access token that dies in an hour and cannot be renewed.
SCOPES = (
    "offline read:profile read:recovery read:cycles "
    "read:sleep read:workout read:body_measurement"
)

PAGE = """<!doctype html><meta charset=utf-8>
<title>WHOOP connected</title>
<body style="font:16px/1.6 system-ui;background:#0d1117;color:#e6edf3;
             display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center">
  <div style="font-size:44px">{icon}</div>
  <h1 style="font-size:20px;margin:.4em 0">{title}</h1>
  <p style="color:#8b949e">{body}</p>
</div>
"""

result = {}
done = threading.Event()
CALLBACK_PATH = "/callback"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        q = urllib.parse.parse_qs(parsed.query)
        result["code"] = (q.get("code") or [None])[0]
        result["state"] = (q.get("state") or [None])[0]
        result["error"] = (q.get("error") or [None])[0]

        ok = bool(result["code"]) and not result["error"]
        page = PAGE.format(
            icon="&#10003;" if ok else "&#10007;",
            title="Connected" if ok else "Authorization failed",
            body="You can close this tab and return to the terminal."
            if ok
            else (result["error"] or "No authorization code returned."),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())
        done.set()

    def log_message(self, *_args):
        pass  # keep the terminal clean


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code == 403 and "1010" in detail:
            raise SystemExit(
                "Token exchange blocked by Cloudflare (error 1010), not by WHOOP.\n"
                "  The request went out with a User-Agent Cloudflare rejects.\n"
                "  This should not happen -- UA is set above; check it was not "
                "stripped."
            )
        raise SystemExit("Token exchange failed: HTTP %s\n%s" % (e.code, detail))


def load_env_file(path=".env.local"):
    """Read KEY=value pairs from .env.local into the environment.

    Lets you keep credentials in one gitignored file instead of exporting them
    into your shell, where they leak into shell history and into the
    environment of every command you run afterwards.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main():
    load_env_file()
    client_id = os.environ.get("WHOOP_CLIENT_ID")
    client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
    # getpass, not input: a typed secret must not land in terminal scrollback.
    if not client_id:
        client_id = getpass.getpass("WHOOP_CLIENT_ID (hidden): ").strip()
    if not client_secret:
        client_secret = getpass.getpass("WHOOP_CLIENT_SECRET (hidden): ").strip()
    if not client_id or not client_secret:
        raise SystemExit("Both WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET are required.")

    state = secrets.token_urlsafe(24)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )

    parsed_redirect = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed_redirect.port or 80
    global CALLBACK_PATH
    CALLBACK_PATH = parsed_redirect.path or "/callback"
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(
            "Cannot bind localhost:%d (%s).\n"
            "  Free the port, or set WHOOP_REDIRECT_URI to a port you did "
            "register." % (port, e)
        )

    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("\nOpening WHOOP authorization in your browser...")
    print("If it does not open, paste this URL yourself:\n\n%s\n" % auth_url)
    webbrowser.open(auth_url)

    if not done.wait(timeout=300):
        raise SystemExit("Timed out after 5 minutes waiting for the redirect.")
    server.shutdown()

    if result.get("error"):
        raise SystemExit("WHOOP returned an error: %s" % result["error"])
    if result.get("state") != state:
        raise SystemExit("State mismatch -- possible CSRF. Aborting.")
    if not result.get("code"):
        raise SystemExit("No authorization code in the redirect.")

    tokens = post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
        },
    )

    refresh = tokens.get("refresh_token")
    if not refresh:
        raise SystemExit(
            "No refresh_token returned. Confirm the `offline` scope is enabled "
            "on your app at developer.whoop.com, then rerun."
        )

    # Deliberately NOT printed. Terminal output gets scrolled back, screen-shared,
    # copied into bug reports and read by anything watching the session. The
    # file is gitignored and owner-read-only.
    path = ".env.local"
    with open(path, "w") as f:
        f.write("# WHOOP credentials. Gitignored -- never commit this file.\n")
        f.write("WHOOP_CLIENT_ID=%s\n" % client_id)
        f.write("WHOOP_CLIENT_SECRET=%s\n" % client_secret)
        f.write("WHOOP_REFRESH_TOKEN=%s\n" % refresh)
    os.chmod(path, 0o600)

    print("\n" + "=" * 68)
    print("Success. Credentials written to %s (chmod 600, gitignored)."
          % os.path.abspath(path))
    print("=" * 68)
    print("""
Nothing was printed to this terminal on purpose -- open the file yourself.

Copy each value into GitHub as a repository secret:
  Settings -> Secrets and variables -> Actions -> New repository secret

  WHOOP_CLIENT_ID
  WHOOP_CLIENT_SECRET
  WHOOP_REFRESH_TOKEN

The access token expires in %ss and is refreshed automatically, so it is not
stored. After the workflow runs once it rewrites WHOOP_REFRESH_TOKEN itself.
""" % tokens.get("expires_in", "?"))


if __name__ == "__main__":
    sys.exit(main())
