"""CLI interface — main entry point for gsuite2router."""

import os
import sys
import time
import random
import argparse

from . import __version__
from .config import (
    DEFAULT_ROUTER_URL,
    DEFAULT_ROUTER_PASSWORD,
    DEFAULT_REDIRECT_URI,
    DEFAULT_DELAY,
    TIMING,
    load_config,
    save_config,
    get_config_value,
)
from .accounts import read_accounts, remove_account
from .router_api import RouterAPI
from .google_auth import google_login, kill_zombie_browsers, clean_exception
from .delete import run_delete

# How many times to attempt one provider for one account before giving up.
# Attempt 1 = fresh authorize → browser → exchange. Attempt 2 (only when the
# failure looks transient or the auth code went stale) repeats the whole
# cycle with a brand-new OAuth session.
MAX_PROVIDER_ATTEMPTS = 2


def format_duration(seconds):
    """Format seconds into readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"

def get_provider_display_name(p):
    """Return formatted display name for provider."""
    names = {
        "antigravity": "Antigravity",
        "cline": "Cline",
        "kilocode": "Kilo Code",
    }
    return names.get(p, p.capitalize())

def print_banner():
    print(r"""
    ___          __  _ ______                 _ __
   /   |  ____  / /_(_) ____/________ __   __(_) /___  __
  / /| | / __ \/ __/ / / __/ ___/ __ `/ | / / / __/ / / /
 / ___ |/ / / / /_/ / /_/ / /  / /_/ /| |/ / / /_/ /_/ /
/_/  |_/_/ /_/\__/_/\____/_/   \__,_/ |___/_/\__/\__, /
                                                 /____/
    """)


def _is_session_error(msg):
    """True if an API error message looks like an expired 9Router session."""
    m = (msg or "").lower()
    return any(
        k in m
        for k in (
            "401", "403", "unauthorized", "forbidden",
            "session expired", "not logged in", "invalid token",
            "auth_token", "login required",
        )
    )


def _is_stale_code_error(msg):
    """True if exchange failed because the auth code went stale (worth one fresh cycle)."""
    m = (msg or "").lower()
    return any(
        k in m
        for k in (
            "invalid_grant", "expired", "already redeemed", "already used",
            "code_verifier", "pkce", "state mismatch", "invalid code",
        )
    )


def _ensure_login(api):
    """Re-login to 9Router. Raises the login error if it fails."""
    api.login()


def _start_oauth_with_retry(api, redirect_uri, provider, p_display):
    """Authorize with one retry + session-expiry relogin. Returns oauth dict."""
    last_err = None
    for attempt in range(1, 3):
        try:
            return api.start_oauth(redirect_uri, provider=provider)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            msg = str(e)
            if _is_session_error(msg):
                print(f"  [API] Session expired — re-logging in...")
                try:
                    _ensure_login(api)
                except Exception as login_err:
                    raise Exception(f"9Router re-login failed: {login_err}")
                continue
            if attempt < 2:
                print(f"  [API] Authorize failed ({clean_exception(e)}) — retrying...")
                time.sleep(1 + random.uniform(0, 0.5))
                continue
            raise
    raise last_err


def _exchange_with_relogin(api, redirect_uri, auth_code, oauth, provider):
    """Exchange auth code; relogins once on session expiry. Returns result dict."""
    try:
        return api.exchange_token(
            redirect_uri,
            auth_code,
            oauth.get("codeVerifier"),
            oauth.get("state"),
            provider=provider,
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        if _is_session_error(str(e)):
            print(f"  [API] Session expired — re-logging in...")
            _ensure_login(api)
            return api.exchange_token(
                redirect_uri,
                auth_code,
                oauth.get("codeVerifier"),
                oauth.get("state"),
                provider=provider,
            )
        raise


def _process_provider(api, email, password, timing, redirect_uri, provider):
    """Run one provider end-to-end. Returns (ok, conn_id_or_error).

    Retries the full authorize → browser → exchange cycle once when the
    failure looks like a stale single-use auth code or a transient error.
    """
    p_display = get_provider_display_name(provider)
    last_err = "unknown error"
    for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
        try:
            print(f"  [API] OAuth authorize ({provider})...")
            oauth = _start_oauth_with_retry(api, redirect_uri, provider, p_display)
            flow_type = oauth.get("flowType", "code")

            if flow_type == "device_code":
                conn_id = google_login(
                    oauth["authUrl"],
                    email,
                    password,
                    timing,
                    redirect_uri,
                    provider=provider,
                    flow_type=flow_type,
                    device_code=oauth.get("deviceCode"),
                    code_verifier=oauth.get("codeVerifier"),
                    router_api=api,
                )
                return True, conn_id

            # Code flow: capture the code in the browser, then exchange
            # immediately — auth codes are single-use with a ~60s lifetime.
            auth_code = google_login(
                oauth["authUrl"],
                email,
                password,
                timing,
                redirect_uri,
                provider=provider,
                flow_type=flow_type,
            )
            print(f"  [API] Exchange token ({provider})...")
            result = _exchange_with_relogin(
                api, redirect_uri, auth_code, oauth, provider
            )
            conn = (result or {}).get("connection", {}) or {}
            return True, conn.get("id", "OK")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = clean_exception(e)
            if attempt < MAX_PROVIDER_ATTEMPTS and (
                _is_stale_code_error(last_err) or "request error" in last_err.lower()
            ):
                print(f"  [RETRY] {p_display} attempt {attempt} failed: {last_err}")
                print(f"  [RETRY] Fresh OAuth session ({attempt + 1}/{MAX_PROVIDER_ATTEMPTS})...")
                continue
            return False, last_err
    return False, last_err


def cmd_init(args):
    """Initialize config with router URL and password."""
    print_banner()

    url = args.url or input(f"Router URL [{DEFAULT_ROUTER_URL}]: ").strip() or DEFAULT_ROUTER_URL
    password = args.password or input(f"Router Password [{DEFAULT_ROUTER_PASSWORD}]: ").strip() or DEFAULT_ROUTER_PASSWORD

    config = load_config()
    config["url"] = url
    config["password"] = password
    save_config(config)

    print(f"\nSaved:")
    print(f"  URL      : {url}")
    print(f"  Password : {'*' * len(password)}")


def cmd_add(args):
    """Add accounts to 9Router providers (Antigravity, Cline, Kilo Code)."""

    akun_file = args.file or os.path.join(os.getcwd(), "akun.txt")
    router_url = get_config_value("url", args.url, DEFAULT_ROUTER_URL)
    router_password = get_config_value("password", args.password, DEFAULT_ROUTER_PASSWORD)
    redirect_uri = args.redirect_uri
    speed_mode = "fast" if args.fast else "normal"
    timing = TIMING[speed_mode]

    provider_arg = get_config_value("provider", getattr(args, "provider", None), "antigravity")
    if provider_arg == "all":
        target_providers = ["antigravity", "cline", "kilocode"]
    elif provider_arg == "both":
        target_providers = ["antigravity", "cline"]
    elif provider_arg in ("antigravity", "cline", "kilocode"):
        target_providers = [provider_arg]
    else:
        target_providers = ["antigravity"]
    if not os.path.isabs(akun_file):
        akun_file = os.path.abspath(akun_file)

    providers_label = ", ".join(get_provider_display_name(p) for p in target_providers)

    print("=" * 55)
    print(f" Router URL    : {router_url}")
    print(f" Provider(s)   : {providers_label}")
    print(f" Speed mode    : {speed_mode.upper()}")
    print(f" Delay         : {args.delay}s")
    print(f" Account file  : {akun_file}")
    print(f" Mode          : API + Browser (Google OAuth only)")
    print("=" * 55)

    kill_zombie_browsers()

    accounts = read_accounts(akun_file)
    if not accounts:
        print("\n [INFO] No accounts to process.")
        print("        Make sure the account file exists and contains: email|password")
        sys.exit(1)

    print(f"\n Total accounts: {len(accounts)}\n")

    api = RouterAPI(router_url, router_password)
    try:
        api.login()
    except Exception as e:
        print(f"\n [ERROR] {e}")
        sys.exit(1)

    success = 0
    fail = 0
    start_time = time.time()

    try:
        for i, account in enumerate(accounts):
            email = account["email"]
            password = account["password"]
            acc_start = time.time()

            print(f"\n{'=' * 55}")
            print(f" [{i + 1}/{len(accounts)}] {email}")
            print(f"{'=' * 55}")

            provider_results = {}
            for p_idx, provider in enumerate(target_providers):
                p_display = get_provider_display_name(provider)
                if len(target_providers) > 1:
                    print(f"\n  --- [{p_idx + 1}/{len(target_providers)}] {p_display} ---")

                try:
                    ok, val = _process_provider(
                        api, email, password, timing, redirect_uri, provider,
                    )
                    if ok:
                        provider_results[provider] = {"ok": True, "conn_id": val}
                        print(f"  [OK] {p_display} — {val}")
                    else:
                        provider_results[provider] = {"ok": False, "error": val}
                        print(f"  [FAIL] {p_display}: {val}")
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    err_msg = clean_exception(e)
                    provider_results[provider] = {"ok": False, "error": err_msg}
                    print(f"  [FAIL] {p_display}: {err_msg}")

            all_ok = all(res["ok"] for res in provider_results.values())
            any_ok = any(res["ok"] for res in provider_results.values())
            acc_elapsed = f"{time.time() - acc_start:.1f}s"

            if all_ok:
                success += 1
                providers_str = " & ".join(
                    get_provider_display_name(p)
                    for p in target_providers
                )
                print(f"\n  [OK] {email} — {providers_str} added ({acc_elapsed})")
                try:
                    remove_account(akun_file, account["raw"])
                except Exception:
                    print(f"  [WARN] Failed to remove from account file (permission?)")
            elif any_ok:
                success += 1
                succeeded = [get_provider_display_name(p) for p, r in provider_results.items() if r["ok"]]
                failed = [get_provider_display_name(p) for p, r in provider_results.items() if not r["ok"]]
                print(f"\n  [PARTIAL] {email} — Succeeded: {', '.join(succeeded)} | Failed: {', '.join(failed)} ({acc_elapsed})")
            else:
                fail += 1
                errors = "; ".join(f"{get_provider_display_name(p)}: {r['error']}" for p, r in provider_results.items())
                print(f"\n  [FAIL] {email}: {errors} ({acc_elapsed})")

            if i < len(accounts) - 1 and args.delay > 0:
                nap = max(0.0, args.delay + random.uniform(-0.3, 0.7))
                print(f"\n  [DELAY] Waiting {nap:.1f}s...")
                time.sleep(nap)

    except KeyboardInterrupt:
        print(f"\n\n [INTERRUPTED] Stopped by user (Ctrl+C)")

    finally:
        try:
            api.close()
        except Exception:
            pass

    total_elapsed = format_duration(time.time() - start_time)
    print(f"\n{'=' * 55}")
    print(f" DONE!")
    print(f" Total   : {len(accounts)} accounts")
    print(f" Success : {success} accounts")
    print(f" Failed  : {fail} accounts")
    print(f" Duration: {total_elapsed}")
    print(f"{'=' * 55}")

def cmd_delete(args):
    """Delete exhausted Antigravity connections from 9Router."""
    print_banner()

    router_url = get_config_value("url", args.url, DEFAULT_ROUTER_URL)
    router_password = get_config_value("password", args.password, DEFAULT_ROUTER_PASSWORD)

    api = RouterAPI(router_url, router_password)
    try:
        api.login()
    except Exception as e:
        print(f"\n [ERROR] {e}")
        sys.exit(1)
    print()

    try:
        run_delete(api, dry_run=args.dry_run)
    finally:
        try:
            api.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        prog="gsuite2router",
        description="Auto Add GSuite accounts to 9Router Antigravity and Cline providers",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # --- init subcommand ---
    p_init = sub.add_parser("init", help="Initialize config (URL and password)")
    p_init.add_argument("--url", default=None, help="9Router URL")
    p_init.add_argument("--password", default=None, help="9Router password")

    # --- add subcommand ---
    p_add = sub.add_parser("add", help="Add accounts to 9Router providers (Antigravity, Cline, Kilo Code)")
    p_add.add_argument("--url", default=None, help="9Router URL (overrides config)")
    p_add.add_argument("--password", default=None, help="9Router password (overrides config)")
    p_add.add_argument("--file", default=None, help="Path to account file (default: akun.txt in CWD)")
    p_add.add_argument("--provider", choices=["both", "all", "antigravity", "cline", "kilocode"], default="antigravity", help="Provider to login: antigravity (default), both (Antigravity and Cline), all (Antigravity, Cline, Kilo Code), cline, or kilocode")
    p_add.add_argument("--fast", action="store_true", help="Fast mode (good internet, minimal Google delays)")
    p_add.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay between accounts in seconds (default: {DEFAULT_DELAY})")
    p_add.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI, help=f"OAuth redirect URI (default: {DEFAULT_REDIRECT_URI})")
    # --- delete subcommand ---
    p_del = sub.add_parser("delete", help="Delete exhausted connections from Antigravity")
    p_del.add_argument("--url", default=None, help="9Router URL (overrides config)")
    p_del.add_argument("--password", default=None, help="9Router password (overrides config)")
    p_del.add_argument("--dry-run", action="store_true", help="Scan only, do not delete")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "delete":
        cmd_delete(args)
    else:
        parser.print_help()
        sys.exit(1)
