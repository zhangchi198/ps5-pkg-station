"""Safety for a web UI that has no login by design (standard library only; the ASGI middleware needs no framework).

What is enforced, and why:

* The Host header must be a name the app is meant to be reached by (an IP address, `localhost`, a single-label
  name, `.local` / `.lan` / `.home.arpa` / `.ts.net` / `.internal` names, plus PS5PKG_ALLOWED_HOSTS). A page on
  somebody's domain that is re-pointed at this machine (DNS rebinding) arrives with its own name and is refused.
* Every request that changes something (POST, PUT, PATCH, DELETE) must come from this page: an Origin (or, when
  there is none, a Referer) that is the host it was sent to, and a JSON Content-Type, which a web page of another
  site can't send without a CORS preflight that this app never answers. No CORS header is ever sent.
* Optional password (PS5PKG_PASSWORD): a login page and a signed session cookie (HttpOnly, SameSite=Strict).
  /api/health stays open for the container's health check.
* Response headers: a Content-Security-Policy that allows no inline script, no framing, no plugins.
* A warning when the request looks like it came from outside the network (see `exposure`).

Everything here is a pure function of its arguments (and the environment) so it can be tested without a server."""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
# names nobody on the internet can point at a private address (DNS rebinding needs a name the attacker owns)
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".ts.net", ".internal", ".localhost", ".localdomain")
OPEN_API = ("/api/health",)                 # no login needed: the container's health check
LOGIN_PATH = "/login"
COOKIE = "ps5pkg_session"
SESSION_DAYS = 30
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
README_URL = "https://github.com/zhangchi198/ps5-pkg-station#keeping-it-private"
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host", "x-forwarded-proto", "cf-connecting-ip", "true-client-ip")


# ---------------------------------------------------------------- settings from the environment

def _words(text):
    return [w.strip().lower().rstrip(".") for w in (text or "").replace(";", ",").replace(" ", ",").split(",") if w.strip()]


def allowed_hosts(environ=None):
    """The names PS5PKG_ALLOWED_HOSTS adds (comma or space separated; a port is ignored): `ps5.example.com`,
    or `*.example.com` / `.example.com` for any name below a domain."""
    out = []
    for w in _words((environ if environ is not None else os.environ).get("PS5PKG_ALLOWED_HOSTS")):
        w = host_name(w)
        if w:
            out.append(w)
    return out


def password(environ=None):
    """The password the page asks for, or "" (no login: the default)."""
    return (environ if environ is not None else os.environ).get("PS5PKG_PASSWORD", "")


def warning_enabled(environ=None):
    return (environ if environ is not None else os.environ).get("PS5PKG_EXPOSURE_WARNING", "1").strip().lower() not in ("0", "off", "false", "no")


# ---------------------------------------------------------------- host names and origins

def host_name(value):
    """The host of a Host header or an origin's netloc, lower case, without port, brackets or trailing dot."""
    h = (value or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _is_ip(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _listed(name, extra):
    for w in extra:
        if w.startswith("*."):
            w = w[1:]
        if w.startswith("."):
            if name.endswith(w):
                return True
        elif name == w:
            return True
    return False


def host_allowed(host_header, extra=()):
    """Is this Host one the app should answer to? (No Host at all can't be a browser: let it through.)"""
    if host_header is None:
        return True
    name = host_name(host_header)
    if not name or len(name) > 255:
        return False
    return (_is_ip(name) or "." not in name or name.endswith(LOCAL_SUFFIXES) or _listed(name, extra))


def _host_port(netloc_or_origin, scheme=None):
    """(host, port) of a Host header (scheme None) or an origin; the default port of the scheme when there is none."""
    try:
        s = urlsplit(netloc_or_origin if "//" in netloc_or_origin else "//" + netloc_or_origin)
        host, port = (s.hostname or "").rstrip("."), s.port
    except ValueError:
        return "", None
    scheme = scheme or s.scheme
    return host, port or {"http": 80, "https": 443}.get(scheme)


def same_origin(url, host_header, extra=()):
    """Does an Origin / Referer value name this server? It does when it is the Host the request was sent to (port
    included; a missing port is the scheme's default), or a name the owner listed in PS5PKG_ALLOWED_HOSTS (a
    reverse proxy that rewrites the Host header still sends the public name as Origin)."""
    if not url or not host_header:
        return False
    try:
        s = urlsplit(url)
        hostname = (s.hostname or "").rstrip(".")
        s.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return False
    if s.scheme not in ("http", "https") or not hostname:
        return False                                    # "null", file:, a sandboxed page
    o_host, o_port = _host_port(url)
    h_host, h_port = _host_port(host_header, s.scheme)
    if (o_host, o_port) == (h_host, h_port):
        return True
    return bool(extra) and _listed(hostname.lower(), extra)


def json_content(content_type):
    return (content_type or "").split(";", 1)[0].strip().lower() == "application/json"


# ---------------------------------------------------------------- the checks for one request

def check_request(method, path, headers, extra=()):
    """None when the request may go on, else (status, why) for the three request-level rules (host, origin,
    content type). `headers` maps lower-case names to values."""
    host = headers.get("host")
    if not host_allowed(host, extra):
        return 403, "host"
    if method in UNSAFE:
        if headers.get("sec-fetch-site") == "cross-site":
            return 403, "cross-site"
        origin = headers.get("origin")
        if origin is not None:
            if not same_origin(origin, host, extra):
                return 403, "cross-site"
        elif headers.get("referer") and not same_origin(headers["referer"], host, extra):
            return 403, "cross-site"
        ctype = headers.get("content-type")
        if path == LOGIN_PATH:                           # the login page is a plain form
            if (ctype or "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
                return 415, "content-type"
        elif not json_content(ctype):
            return 415, "content-type"
    return None


# ---------------------------------------------------------------- the password and the session cookie

_secrets: dict = {}
_secret_lock = threading.Lock()


def _secret(state_dir):
    """A random value kept in the state folder (mode 0600), so a session survives a restart; a read-only folder
    gets one that lives as long as the process."""
    key = str(state_dir)
    with _secret_lock:
        if key in _secrets:
            return _secrets[key]
        path = Path(state_dir) / "session.secret"
        try:
            val = bytes.fromhex(path.read_text().strip())
            if len(val) < 16:
                raise ValueError
        except (OSError, ValueError):
            val = secrets.token_bytes(32)
            try:
                tmp = path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(val.hex())
                os.replace(tmp, path)
            except OSError:
                pass
        _secrets[key] = val
        return val


def _sign(state_dir, pw, payload):
    key = hmac.new(_secret(state_dir), pw.encode("utf-8"), hashlib.sha256).digest()      # a new password ends every session
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def password_ok(candidate, pw):
    """Constant-time comparison."""
    a, b = hashlib.sha256((candidate or "").encode("utf-8")).digest(), hashlib.sha256(pw.encode("utf-8")).digest()
    return bool(pw) and hmac.compare_digest(a, b)


def make_session(state_dir, pw, now=None):
    exp = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    payload = f"v1.{exp}"
    return f"{payload}.{_sign(state_dir, pw, payload)}"


def session_valid(value, state_dir, pw, now=None):
    try:
        ver, exp, sig = (value or "").split(".")
        if ver != "v1" or int(exp) < (now if now is not None else time.time()):
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sig, _sign(state_dir, pw, f"{ver}.{exp}"))


def cookie_value(cookie_header, name=COOKIE):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


class LoginThrottle:
    """At most `limit` wrong passwords per client address per `window` seconds; then the page says to wait."""

    def __init__(self, limit=8, window=300):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, who, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            recent = [t for t in self.fails.get(who, []) if now - t < self.window]
            self.fails[who] = recent
            return len(recent) >= self.limit

    def fail(self, who, now=None):
        with self.lock:
            self.fails.setdefault(who, []).append(now if now is not None else time.time())

    def ok(self, who):
        with self.lock:
            self.fails.pop(who, None)


# ---------------------------------------------------------------- is this page open to the world?

def _address(ip):
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return None
    return getattr(a, "ipv4_mapped", None) or a


def is_local_address(ip, server_ip=None):
    """Private, loopback, link-local and Tailscale (100.64.0.0/10) addresses are "inside"; a global one is not,
    unless it is an IPv6 address in the same /64 as the address the request was sent to (a home network with
    IPv6 addresses from the provider: the PC and the NAS are on one link)."""
    a = _address(ip)
    if a is None:
        return True                                     # unknown (a unix socket, a test client): nothing to say
    if not a.is_global:
        return True
    s = _address(server_ip)
    return bool(s is not None and a.version == 6 and s.version == 6 and ipaddress.ip_network(f"{s}/64", strict=False).supernet_of(ipaddress.ip_network(f"{a}/128")))


def exposure(client_ip, headers, *, login, enabled=True, server_ip=None):
    """What the page should say about who can reach it: {login, warn, reason, client}. `warn` is only for a page
    with no password: the request came from a global address (a port forward), or carries the headers of a reverse
    proxy (the owner knows whether that proxy is open to the internet; the page can't)."""
    out = dict(login=bool(login), warn=False, reason=None, client=None)
    if login or not enabled:
        return out
    if not is_local_address(client_ip, server_ip):
        out.update(warn=True, reason="remote", client=client_ip)
    elif any(h in headers for h in PROXY_HEADERS):
        out.update(warn=True, reason="proxy")
    return out


# ---------------------------------------------------------------- pages

def blocked_host_page(host, accept_html=True):
    """What a refused Host gets: (content type, body) saying which variable to set."""
    shown = (host or "").strip()[:200]
    name = host_name(shown) or shown
    msg = (f"This address ({shown}) is not one this app answers to. It answers to IP addresses, localhost, single-word "
           f"names and names ending in .local, .lan, .home.arpa, .ts.net or .internal. To use {name}, add it to "
           f"PS5PKG_ALLOWED_HOSTS (a comma separated list of names), for example PS5PKG_ALLOWED_HOSTS={name}, in the "
           f"environment of the container (docker-compose.yml) and restart it. This protects against web pages "
           f"that borrow your network to talk to this app.")
    if not accept_html:
        return "application/json", json.dumps({"detail": msg})
    return "text/html; charset=utf-8", (
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Address not allowed</title><body style='font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em'>"
        f"<h1>Address not allowed</h1><p>{html.escape(msg)}</p>"
        f"<p>Set it like this:</p><pre style='background:#eee;padding:.7em;overflow:auto'>environment:\n  PS5PKG_ALLOWED_HOSTS: {html.escape(name)}</pre>"
        f"<p><a href=\"{README_URL}\" rel=\"noopener noreferrer\">More in the README</a></p></body></html>")


def login_page(error=None):
    note = f"<p style='color:#b91c1c'>{html.escape(error)}</p>" if error else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>PS5 PKG Station - log in</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;background:#f6f7f9;color:#1c2330;display:grid;place-items:center;min-height:90vh;margin:0}"
        "@media (prefers-color-scheme:dark){body{background:#0f1318;color:#e6e9ee}}"
        "form{background:Canvas;color:CanvasText;border:1px solid #8884;border-radius:10px;padding:1.2em 1.5em;width:min(22em,92vw)}"
        "input,button{font:inherit;width:100%;padding:.5em;margin:.4em 0;box-sizing:border-box}button{cursor:pointer}h1{font-size:1.1em;margin:0 0 .5em}</style>"
        f"<body><form method=post action=\"{LOGIN_PATH}\"><h1>PS5 PKG Station</h1>{note}"
        "<label>Password<input type=password name=password autofocus autocomplete=current-password required></label>"
        "<button type=submit>Log in</button></form></body></html>")


# ---------------------------------------------------------------- the ASGI middleware

def _headers(scope):
    out = {}
    for k, v in scope.get("headers") or []:
        out.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
    return out


async def _respond(send, status, ctype, body, extra=()):
    data = body.encode() if isinstance(body, str) else body
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(data)).encode()), *extra]})
    await send({"type": "http.response.body", "body": data})


REASONS = {
    "cross-site": "This request did not come from this page (its Origin or Referer is another site), so it was refused. "
                  "If you reach the app through a reverse proxy that changes the Host header, list the public name in PS5PKG_ALLOWED_HOSTS.",
    "content-type": "Requests that change something must be sent with Content-Type: application/json "
                    "(for curl: -H 'Content-Type: application/json').",
}


class Guard:
    """Wraps an ASGI app: the host / origin / content-type rules, the password, and the security headers. The
    request's login state is left in scope["ps5_authed"] for the handlers; `state_dir` is a function so a test can
    point it somewhere else."""

    def __init__(self, app, state_dir):
        self.app, self.state_dir = app, state_dir

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers, path, method = _headers(scope), scope.get("path", "/"), scope["method"]
        extra = allowed_hosts()
        bad = check_request(method, path, headers, extra)
        wants_html = "text/html" in headers.get("accept", "") and not path.startswith("/api/")
        if bad:
            status, why = bad
            if why == "host":
                ctype, body = blocked_host_page(headers.get("host"), accept_html=wants_html)
            else:
                ctype, body = "application/json", json.dumps({"detail": REASONS[why]})
            return await _respond(send, status, ctype, body, self.security_headers())
        pw = password()
        authed = not pw or session_valid(cookie_value(headers.get("cookie")), self.state_dir(), pw)
        scope["ps5_authed"] = authed
        if pw and not authed and path.startswith("/api/") and path not in OPEN_API and path != "/api/logout":
            return await _respond(send, 401, "application/json", json.dumps({"detail": "Log in first."}), self.security_headers())

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                add = [(k, v) for k, v in self.security_headers() if k not in have]
                if path.startswith("/api/") and b"cache-control" not in have:
                    add.append((b"cache-control", b"no-store"))
                message = dict(message, headers=[*message.get("headers", []), *add])
            await send(message)
        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def security_headers():
        return [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin")]
