# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub Security Advisories: open this repository's **Security**
tab and choose **Report a vulnerability** (https://github.com/tonyyang0504/harvest/security/advisories/new). Do not
open a public issue, pull request or discussion for a vulnerability.

Include what you found, how to reproduce it, the impact you expect and the version or commit. You will get an
acknowledgement within 7 days and a fix or a plan within 30 days for confirmed issues; we coordinate disclosure with
you and credit you in the advisory unless you prefer otherwise.

## Supported versions

| version | supported |
|---|---|
| 0.1.x (main) | yes |
| anything older | no |

## Threat model in brief

harvest runs scraper modules written by agents and fetches pages chosen by agents, so both are untrusted:

- **Modules** pass a review gate (AST policy lint, a sandboxed page-1 fetch, schema validation; the verdict is bound to
  the module's sha256) and run in a separate process: under bubblewrap on Linux (`HARVEST_SANDBOX=bwrap`; no network of
  their own, a read-only filesystem, harvest's state and secrets masked), and under an audit-hook policy layer elsewhere.
- **Fetching** refuses private, loopback, link-local and metadata addresses (connections pinned to the vetted address),
  honours robots.txt, and never works around a bot challenge, login, captcha or paywall.
- **Interfaces**: the web app binds 127.0.0.1 and needs an admin token; agent jobs run with explicit tool allowlists;
  repair dispatch is off by default.
- **Secrets** (admin token, proxy pool file, MCP-lane config, store DSN) are read in place and never logged.

The October 2026 review and its regression tests: [docs/SECURITY_REVIEW_2026-10.md](docs/SECURITY_REVIEW_2026-10.md).
