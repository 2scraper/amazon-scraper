# Security Policy

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository. It opens a channel only you and the maintainers can see, so nothing
is public until there is a fix.

Please do not open a normal issue for anything in the list below — issues are
public from the moment you press submit.

If private reporting is unavailable to you, mail support@2captcha.com. That is
2Captcha's general support address rather than a security-only one, so put
**"amazon-scraper security"** in the subject — otherwise it lands in a queue
about API keys and billing and takes longer to reach the right person.

**What helps most:** the version you are on (commit hash), the exact command,
what an attacker gains, and the smallest input that shows it. If the report
involves credentials, replace them with `***` — the same rule as everywhere else
in this project.

We aim to acknowledge a report within three working days and to say plainly
whether we consider it in scope. If a fix is warranted we will credit you in the
commit unless you would rather we did not.

## What is in scope

This repository is a command-line scraper. It has no server, no accounts and no
users other than the person running it, so the interesting failures are about
**credentials leaking** and about **untrusted input from the sites being
scraped**.

In scope:

- **A credential reaching somewhere it should not.** The engines mask
  `user:pass@` in their own log lines and read keys from the environment rather
  than `argv`, precisely because a secret in `argv` is visible to anything that
  can run `ps`. A path we have missed — a log line, an exception message, a
  written file, a request to a third party — is a real bug and we want to know.
- **Anything that makes a scraped page dangerous to parse.** The parser is
  handed HTML from a site we do not control. Remote code execution, path
  traversal via a crafted URL or filename, or a catastrophic regex backtrack
  that a page can trigger deliberately all count.
- **Injection into a page we drive.** Values fetched from an API are
  JSON-encoded before they are interpolated into an init script, so that a
  string cannot break out of it. A place where that is not true is in scope.
- **A dependency vulnerability that is actually reachable** through the way this
  code uses the library. Say which call path reaches it.
- **`.gitignore` failing to cover something this project writes** that could
  contain a secret, so it can be committed by accident.

## What is not in scope

Not because these do not matter, but because they belong somewhere else:

- **Bypassing Amazon's bot protection.** This scraper drives an ordinary
  browser and passes challenges the way a browser does. Anything about how
  Akamai or reCAPTCHA behave is not a vulnerability in this repository.
- **The scraper stopped working.** Amazon changing its markup is expected —
  file it as a normal issue, there is a template for exactly that.
- **Anything about 2Captcha's services** — the solver API, the Scraping Browser
  API, proxies, fingerprints, billing, quotas. This repository is only a client
  of those; it cannot fix them, and we cannot see your account.
  - A vulnerability in a service → <https://2captcha.com/support>
  - A service behaving differently from how you expected → the API reference
    first: <https://2captcha.com/api-docs>. Task types, parameters and error
    codes are all specified there, and most "this looks like a bug" reports
    about the solver turn out to be a parameter set that does not match the
    captcha variant.
- **A dependency advisory with no reachable path here**, pasted from a scanner.
  Several of our optional dependencies carry advisories for code paths this
  project never calls; a report needs to show the path.
- **Rate limits, terms of service, or the legality of scraping** in your
  jurisdiction. See the Legal section of the README — those are your
  responsibility as the operator, not defects.
- **Reports generated entirely by an automated tool** with no analysis of
  whether the finding applies. We read every report, and unexamined scanner
  output takes time away from ones that matter.

## Supported versions

`main` only. This project has no releases or version tags; fixes land on `main`
and you update by pulling. If you are running an old clone, update before
reporting.

## If you have leaked a key

This is by far the most likely security event for anyone using this project, and
it is recoverable. In order:

1. **Rotate the key** in the 2Captcha dashboard. Do this first — it invalidates
   the leaked value immediately, whatever else happens.
2. **Rotate the Scraping Browser zone password** if a full
   `ws://user:pass@cb.2captcha.com:9222` string was exposed.
3. Only then worry about the commit or the log. Deleting a commit does not help:
   anything pushed to a public repository should be treated as public
   permanently, whether or not the commit still exists.

Three places leak credentials that people do not expect, because unlike our own
log lines they are **not** masked:

- **raw HTML dumps** (`--dump-html`, and the automatic dump on a zero-product
  run) — these can contain session cookies
- **the Scraper API's `x-debug` response header**
- **your shell history**, if you passed a key on the command line

Use `.env` for credentials. It is in `.gitignore`, and `python3 env_config.py`
reports what is configured without printing any values.
