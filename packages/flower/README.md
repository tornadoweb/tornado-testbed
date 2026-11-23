# flower

Celery monitoring web UI, ~6.5k stars; Flower is a Tornado application.

## What it exercises

The most complete "classic Tornado app" usage in this set:

- `tornado.auth` for OAuth2/Google login -- **the only package here that
  imports it at all**
- `tornado.options` for CLI argument parsing
- `tornado.template` for HTML rendering (the highest template coverage of any
  entry)
- `tornado.locale` for i18n
- `tornado.testing` as its test base

## Standing limitation: tornado.auth stays at ~18%

Although flower is the only consumer of `tornado.auth`, its committed suite only
covers the `authenticate()` / `validate_auth_option()` helpers and HTTP Basic
auth. Nothing drives the OAuth2 login handlers
(`GoogleOAuth2Mixin.get_authenticated_user`, `authorize_redirect`,
`oauth2_request`), which is where most of `auth.py` lives.

Moving that number needs *new* OAuth-flow tests, not more of what exists, so it
is not something running more of this suite can fix.

## Known failure: cross-origin protection by default (tornado#3809)

[tornado#3809](https://github.com/tornadoweb/tornado/pull/3809), merged for
6.7, turns `cross_origin_protection` on by default. Exactly one flower test
fails under it, and it is listed in `known_failures.toml` as a strict xfail for
`tornado>=6.7.dev1`:

- `tests/unit/views/test_xsrf.py::XsrfProtectionTests::test_unauthenticated_server_allows_cross_site`
  expects a cross-site POST to reach flower's API when no auth is configured
  ("CORS is deliberately open without credentials") and gets Tornado's 403.

The other 352 tests pass, including the rest of `test_xsrf.py` (measured
against tornado master at 73823ed). Flower's own XSRF and fetch-metadata checks
already cover authenticated sessions, so this is flower's choice to stay open
when unauthenticated colliding with the new default. Flower has to opt out
(`cross_origin_protection=False`, or a `check_trusted_origin` override) for that
mode.

When flower ships that opt-out, the test starts passing on master, the strict
xfail turns flower red, and the entry should be deleted. Swap the issue link in
`known_failures.toml` for flower's issue or PR once there is one.
