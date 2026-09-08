"""
env_config.py
--------------
Reads a `.env` file sitting next to the scripts, so credentials live in one
place instead of being retyped into every command line.

Two reasons this is hand-rolled rather than `python-dotenv`:

1. **No new dependency.** Every engine here already refuses to be a heavy
   install, and a credential loader is about thirty lines. If `python-dotenv`
   *is* installed it gets used instead, so a project that already depends on it
   keeps its own behaviour.
2. **A secret in argv is visible to anything that can run `ps`.** Passing
   `--twocaptcha-key sk_live_...` leaks the key to every other process on the
   machine and into shell history. Reading it from the environment does not.

Precedence, highest first:

    explicit CLI flag  >  real environment variable  >  .env file  >  default

That order matters: a `.env` must never silently override something the caller
typed, and an already-exported variable (CI secret, `direnv`, a shell profile)
must never be clobbered by a file someone forgot to delete.

Nothing here is required. With no `.env` and no environment variables the
scripts behave exactly as before.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Recognised keys, and which CLI destination each one backs.
# Keeping this explicit means a typo in .env is reported rather than ignored.
ENV_KEYS = {
    "TWOCAPTCHA_KEY": "twocaptcha_key",
    "AMAZON_CDP_ENDPOINT": "cdp_endpoint",
    "AMAZON_PROXY": "proxy",
    "AMAZON_URL": "url",
}
# Deliberately NOT here: an output prefix. `--out` already carries a non-empty
# default, so `apply()` would never see it as unset and the variable would be
# silently ignored — a setting that looks configurable and is not.

# Values that look like a key but are the placeholder from .env.example.
# A placeholder that reaches the API produces a confusing auth error a long way
# from its cause, so it is caught here instead.
_PLACEHOLDERS = {
    "your_2captcha_api_key_here",
    "your_api_key_here",
    "changeme",
    "",
}

_loaded_from = None


def _parse_line(line):
    """Return (key, value) or None. Understands `export K=V`, quotes, # comments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    # Strip one matching pair of quotes; leave inner ones alone.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    else:
        # Unquoted values may carry a trailing comment.
        value = value.split(" #")[0].strip()
    if not key:
        return None
    return key, value


def load_env(path=None, override=False):
    """Load `.env` into os.environ. Returns the path used, or None.

    `override=False` (the default) means an already-set environment variable
    wins over the file.
    """
    global _loaded_from

    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)

    if not path.is_file():
        return None

    # Defer to python-dotenv when the user already has it: their version may
    # support syntax this parser does not (multi-line values, interpolation).
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=override)
        _loaded_from = str(path)
        logger.debug("Loaded %s via python-dotenv", path)
        return str(path)
    except ImportError:
        pass

    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if not parsed:
            continue
        key, value = parsed
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        count += 1

    _loaded_from = str(path)
    logger.debug("Loaded %d value(s) from %s", count, path)
    return str(path)


def unknown_keys(path=None):
    """Keys present in .env that nothing in this project reads.

    Usually a typo — `TWO_CAPTCHA_KEY`, `AMAZON_CDP` — which otherwise fails
    silently as "the key just isn't being picked up".
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)
    if not path.is_file():
        return []
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if parsed and parsed[0] not in ENV_KEYS:
            found.append(parsed[0])
    return found


def env_value(name):
    """Read one recognised variable, treating .env.example placeholders as unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    if value.strip().lower() in _PLACEHOLDERS:
        logger.warning(
            "%s is still set to the placeholder from .env.example — treating it "
            "as unset. Put your real value in .env.", name)
        return None
    return value.strip() or None


def apply(args, keys=None, quiet=False):
    """Fill unset argparse destinations from the environment.

    Call once, right after `parse_args()`. Only fills a destination that the
    parser actually defines and that is still falsy, so an explicit flag always
    wins.
    """
    load_env()

    for env_name, dest in (keys or ENV_KEYS).items():
        if not hasattr(args, dest):
            continue
        if getattr(args, dest):
            continue
        value = env_value(env_name)
        if value:
            setattr(args, dest, value)
            if not quiet:
                # Never log the value. Credentials end up in CI logs, pasted
                # terminal output and bug reports.
                logger.info("Using %s from the environment for --%s",
                            env_name, dest.replace("_", "-"))

    if not quiet:
        for key in unknown_keys():
            logger.warning("Ignoring unrecognised key in .env: %s", key)

    return args


if __name__ == "__main__":
    # `python3 env_config.py` — report what is configured, without printing
    # any secret. Useful as a first step when a key "isn't being picked up".
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    where = load_env()
    print(f".env file:      {where or 'not found (this is fine — env vars still work)'}")
    for env_name, dest in ENV_KEYS.items():
        value = env_value(env_name)
        if value is None:
            state = "not set"
        elif "KEY" in env_name or "@" in value:
            state = f"set ({len(value)} chars, hidden)"
        else:
            state = f"set ({value})"
        print(f"  {env_name:<24} -> --{dest.replace('_', '-'):<16} {state}")
    extras = unknown_keys()
    if extras:
        print("\nUnrecognised keys in .env (typo?): " + ", ".join(extras))
