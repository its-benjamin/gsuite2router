"""Google OAuth login via DrissionPage — anti-detection, text-based locators."""

import os
import json
import time
import tempfile
import shutil
import random
from urllib.parse import urlparse, parse_qs
from DrissionPage._units.actions import Keys

from .config import CHROME_ARGS, DEFAULT_REDIRECT_URI


def clean_exception(e):
    """Convert any DrissionPage internal error messages into clean English."""
    msg = str(e).strip()
    if "页面被刷新" in msg or "加载完成" in msg:
        return "Page was navigating/reloading during action"
    if "没有找到" in msg:
        return "Element not found on page"
    if "连接" in msg and "断开" in msg:
        return "Browser connection disconnected"
    if "版本:" in msg:
        msg = msg.split("版本:")[0].strip()
    return msg


# ============================================================
# Small wait / stealth helpers
# ============================================================
def _nap(base, jitter=0.3):
    """Sleep ~base seconds with ±jitter so intervals aren't mechanical."""
    if base is None or base <= 0:
        return
    time.sleep(max(0.05, base + random.uniform(-jitter * base, jitter * base)))


def _safe_url(tab):
    """Return tab.url, or "" while the page is mid-navigation."""
    try:
        return tab.url or ""
    except Exception:
        return ""


def _wait_for(pred, timeout, poll=0.15):
    """Poll pred() until truthy; return its value or None on timeout."""
    deadline = time.time() + timeout
    while True:
        try:
            val = pred()
        except Exception:
            val = None
        if val:
            return val
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def _wait_for_url_change(tab, old_url, timeout=4, poll=0.15):
    """Wait until the page navigates away from old_url. Returns new URL or None."""
    old = (old_url or "").split("#")[0]
    return _wait_for(
        lambda: ((u := _safe_url(tab)) and u.split("#")[0] != old and u) or None,
        timeout,
        poll,
    )


def _wait_for_url_contains(tab, needle, timeout=10, poll=0.2):
    """Wait until the URL contains needle. Returns URL or None."""
    needles = (needle,) if isinstance(needle, str) else tuple(needle)
    return _wait_for(
        lambda: ((u := _safe_url(tab)) and any(n in u for n in needles) and u) or None,
        timeout,
        poll,
    )


def _page_has_text(tab, *phrases):
    """Single-round-trip check whether the page body contains any phrase."""
    try:
        lowered = [str(p).lower() for p in phrases]
        return bool(
            tab.run_js(
                """
                const needles = arguments[0];
                const t = (document.body ? document.body.innerText : '').toLowerCase();
                return needles.some(n => t.includes(n));
                """,
                lowered,
            )
        )
    except Exception:
        return False


# ============================================================
# OAuth URL parsing — code AND error handling
# ============================================================
def _extract_oauth_result(url):
    """Parse an OAuth redirect URL.

    Returns (code, error, error_description). Google puts both the auth
    ``code`` and any ``error`` (e.g. access_denied) in the query string;
    some providers use the fragment — check both.
    """
    if not url:
        return None, None, None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        frag = parse_qs(parsed.fragment)

        def _get(name):
            v = qs.get(name) or frag.get(name)
            return v[0] if v else None

        return _get("code"), _get("error"), _get("error_description")
    except Exception:
        return None, None, None


_OAUTH_ERROR_HINTS = {
    "access_denied": (
        "Access denied at the consent screen — the account declined "
        "(or a required scope was rejected)"
    ),
    "redirect_uri_mismatch": (
        "redirect_uri_mismatch — the redirect URI is not registered on the "
        "OAuth client; pass the exact registered URI via --redirect-uri"
    ),
    "invalid_grant": (
        "Invalid grant — auth code expired (single-use, ~60s) or already "
        "redeemed; retry the account"
    ),
    "unauthorized_client": (
        "Unauthorized client — OAuth client not allowed for this flow"
    ),
    "disallowed_useragent": (
        "Google blocked the browser (disallowed user-agent) — automation "
        "fingerprint flagged; retry with a fresh IP/profile"
    ),
}


def _oauth_error_message(error, description):
    """Human-readable message for an OAuth ?error= redirect."""
    hint = _OAUTH_ERROR_HINTS.get((error or "").lower())
    if hint:
        return f"OAuth error ({error}): {hint}"
    if description:
        return f"OAuth error ({error}): {description}"
    return f"OAuth error ({error})"


def _raise_for_oauth_error(url):
    """Raise with a helpful message if url carries ?error=. Returns the code or None."""
    code, error, desc = _extract_oauth_result(url)
    if error:
        raise Exception(_oauth_error_message(error, desc))
    return code


def _find_element(tab, locators, timeout=1.5):
    """Find element from list of locators, return first found or None."""
    for locator in locators:
        try:
            ele = tab.ele(locator, timeout=timeout)
            if ele:
                return ele
        except Exception:
            continue
    return None


def _find_and_click(tab, locators, timeout=1.5):
    """Find and click element from list of locators."""
    for locator in locators:
        try:
            ele = tab.ele(locator, timeout=timeout)
            if ele:
                try:
                    ele.click()
                    return True
                except Exception:
                    try:
                        ele.click(by_js=True)
                        return True
                    except Exception:
                        continue
        except Exception:
            continue
    return False


def _click_first_button_by_text(tab, *texts):
    """One-round-trip JS click on the first visible button matching any text.

    Faster than enumerating elements from Python (one CDP call instead of N).
    Returns the matched text, or None.
    """
    try:
        return tab.run_js(
            """
            const needles = arguments[0].map(s => s.toLowerCase());
            const btn = Array.from(document.querySelectorAll('button, a, input[type="submit"], div[role="button"]'))
                .find(el => {
                    if (el.offsetParent === null) return false;
                    const t = (el.innerText || el.value || el.textContent || '').toLowerCase().trim();
                    return needles.some(n => t === n || t.includes(n));
                });
            if (btn) {
                btn.scrollIntoView({block: 'center'});
                btn.click();
                return true;
            }
            return null;
            """,
            list(texts),
        )
    except Exception:
        return None


def _force_input(tab, locator_or_ele, text, timeout=10, desc="field"):
    """Input text with 4-layer fallback strategy. Accepts element directly or locator(s)."""
    if hasattr(locator_or_ele, "input"):
        ele = locator_or_ele
    elif isinstance(locator_or_ele, (list, tuple)):
        ele = _find_element(tab, locator_or_ele, timeout=timeout)
    else:
        ele = _find_element(tab, [locator_or_ele], timeout=timeout)

    if ele is None:
        raise Exception(f"Element {desc} not found: {locator_or_ele}")

    def _has_value():
        try:
            val = ele.attr("value") or ele.property("value") or ele.run_js("return this.value;") or ""
            return text in val or len(val) == len(text) or len(val) > 0
        except Exception:
            return False

    # Strategy 1: .input() standard (trusted input events — most stealthy)
    try:
        ele.input(text, clear=True)
        _nap(0.15, jitter=0.5)
        if _has_value():
            return ele
    except Exception:
        pass

    # Strategy 2: .input(by_js=True)
    try:
        ele.input(text, clear=True, by_js=True)
        _nap(0.15, jitter=0.5)
        if _has_value():
            return ele
    except Exception:
        pass

    # Strategy 3: CDP keyboard input
    try:
        ele.click()
        _nap(0.1, jitter=0.5)
        tab.actions.key_down(Keys.CTRL).type("a").key_up(Keys.CTRL)
        tab.actions.type(Keys.BACKSPACE)
        tab.actions.type(text)
        _nap(0.15, jitter=0.5)
        if _has_value():
            return ele
    except Exception:
        pass

    # Strategy 4: Raw JavaScript with full event simulation
    try:
        ele.click()
        _nap(0.1, jitter=0.5)
        ele.run_js(
            """
            this.focus();
            this.value = arguments[0];
            this.dispatchEvent(new Event('input', {bubbles: true}));
            this.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            text,
        )
        _nap(0.15, jitter=0.5)
        return ele
    except Exception:
        pass

    raise Exception(f"Failed to input text to {desc} with all strategies")


def _extract_code_from_url(url):
    """Extract OAuth code from URL query parameter (None if ?error= or absent)."""
    code, error, _ = _extract_oauth_result(url)
    if error:
        return None
    return code


def _check_redirect(tab, redirect_uri):
    """Check if current URL is the callback redirect with auth code."""
    try:
        url = tab.url
        if redirect_uri:
            uri_clean = redirect_uri.split("://", 1)[-1].rstrip("/")
            if uri_clean in url:
                return _extract_code_from_url(url)
        if "code=" in url and ("callback" in url or "localhost" in url):
            return _extract_code_from_url(url)
    except Exception:
        pass
    return None


def _check_google_errors(tab):
    """Check if Google displayed an error or security verification checkpoint."""
    try:
        url = tab.url.lower()
        if "challenge/ipp" in url or "idvpreregistered" in url:
            return "Google security challenge: Phone verification required (IP flagged/rate-limited)"
        if "disabled/intro" in url:
            return "Google account disabled"
        if "challenge/selection" in url:
            return "Google security challenge: 2-Step verification required"
        if "challenge/pk" in url:
            return "Google security challenge: Passkey/Security key required"
        if "challenge/kpe" in url:
            return "Google security challenge: Phone number confirmation required"
        if "radar-challenge" in url:
            return "Security challenge: Phone verification required (WorkOS Radar)"
        err = tab.run_js("""
            const url = window.location.href.toLowerCase();
            if (url.includes('challenge/ipp') || url.includes('idvpreregistered')) {
                return "Google security challenge: Phone verification required (IP flagged/rate-limited)";
            }
            if (url.includes('disabled/intro')) {
                return "Google account disabled";
            }
            if (url.includes('challenge/selection')) {
                return "Google security challenge: 2-Step verification required";
            }

            const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
            // Automation-fingerprint block: Google refuses sign-in from this browser.
            if (bodyText.includes('may not be secure') || bodyText.includes('mungkin tidak aman') ||
                bodyText.includes('disallowed_useragent') || bodyText.includes('access blocked')) {
                return "Google blocked sign-in: This browser or app may not be secure (automation fingerprint flagged — retry with fresh IP/profile)";
            }
            if (bodyText.includes('enter a phone number') || bodyText.includes('masukkan nomor telepon') ||
                bodyText.includes('get a verification code') || bodyText.includes('dapatkan kode verifikasi') ||
                (bodyText.includes("verify it's you") && (bodyText.includes('phone') || bodyText.includes('telepon'))) ||
                bodyText.includes('unusually high number of requests') || bodyText.includes('terlalu banyak permintaan')) {
                return "Google security challenge: Phone verification required (IP flagged/rate-limited)";
            }
            if (bodyText.includes('verify your phone number') || bodyText.includes('valid mobile phone number')) {
                return "Security challenge: Phone verification required (WorkOS Radar)";
            }

            const errKeywords = [
                "tidak dapat menemukan akun google",
                "enter a valid email",
                "masukkan email yang valid",
                "wrong password",
                "sandi salah",
                "couldn't sign you in",
                "tidak bisa login",
                "too many failed attempts",
                "terlalu banyak upaya",
                "account disabled",
                "akun dinonaktifkan",
                "cannot sign you in",
                "tidak dapat membuat anda login",
                "this phone number cannot be used",
                "nomor telepon ini tidak dapat digunakan"
            ];
            const errEls = document.querySelectorAll(
                'div[aria-live="assertive"], div.jssense, div[jsname="B1fAeb"], .Ekjuhf, .dEOOab, .o6cuMc'
            );
            for (const el of errEls) {
                const text = (el.innerText || el.textContent || '').toLowerCase().trim();
                for (const kw of errKeywords) {
                    if (text.includes(kw)) {
                        return (el.innerText || el.textContent || '').trim();
                    }
                }
            }
            return null;
        """)
        return err
    except Exception:
        return None


def _wait_for_password_field(tab, max_timeout=15):
    """Fast poll for the password input (single JS visibility check per tick).

    Returns the element, or None if the page navigated away from sign-in
    (consent/auto-redirect — caller must check for the auth code first).
    Raises immediately on Google error pages.
    """
    deadline = time.time() + max_timeout
    while time.time() < deadline:
        try:
            visible = tab.run_js(
                "const el = document.querySelector('input[type=\"password\"]');"
                " return (el && el.offsetParent !== null) ? true : false;"
            )
            if visible:
                try:
                    # NOTE: bare CSS 'input[type="password"]' is parsed as
                    # xpath by DrissionPage and never matches — use tag syntax.
                    ele = tab.ele('tag:input@@type=password', timeout=0.5)
                    if ele:
                        return ele
                except Exception:
                    pass
        except Exception:
            pass

        # Left Google sign-in entirely (consent / redirect in progress)?
        # Don't burn the full timeout — bail so the caller checks for the code.
        try:
            u = (tab.url or "").lower()
            if u and "accounts.google.com" not in u and "google.com" not in u:
                return None
        except Exception:
            pass

        err = _check_google_errors(tab)
        if err:
            raise Exception(f"Google error: {err}")

        time.sleep(0.15)

    return None


def _wait_for_email_field(tab, max_timeout=8):
    """Wait for the identifier (email) field. True if found, False on timeout/nav-away."""
    deadline = time.time() + max_timeout
    while time.time() < deadline:
        try:
            visible = tab.run_js(
                "const el = document.querySelector('#identifierId, input[type=\"email\"]');"
                " return (el && el.offsetParent !== null) ? true : false;"
            )
            if visible:
                return True
        except Exception:
            pass
        try:
            u = (tab.url or "").lower()
            if u and "accounts.google.com" not in u and "google.com" not in u:
                return False
        except Exception:
            pass
        err = _check_google_errors(tab)
        if err:
            raise Exception(f"Google error: {err}")
        time.sleep(0.15)
    return False


def _handle_consent_loop(tab, timing, redirect_uri=None, email=None, password=None, flow_type="code", provider="antigravity"):
    """Handle Google consent / confirmation / account picker pages until OAuth redirect happens."""
    MAX_STEPS = 25
    tos_accepted = False
    after_btn = timing["after_consent_btn"]
    last_url = ""

    for step in range(1, MAX_STEPS + 1):
        try:
            # Check redirect for auth code (code flow)
            code = _check_redirect(tab, redirect_uri)
            if code:
                return code

            try:
                current_url = tab.url
            except Exception:
                _nap(0.5, jitter=0.5)
                code = _check_redirect(tab, redirect_uri)
                if code:
                    return code
                return None
            last_url = current_url

            # OAuth provider returned an explicit error (?error=...) — fail fast
            # with a helpful message instead of clicking around a dead page.
            _raise_for_oauth_error(current_url)

            # Immediate security challenge check
            err = _check_google_errors(tab)
            if err:
                raise Exception(err)

            # Left Google domain = redirect in progress / completed
            if "accounts.google.com" not in current_url and "google.com" not in current_url:
                if flow_type == "device_code":
                    return "device_auth_return"
                if "radar-challenge" in current_url:
                    raise Exception(f"{provider.capitalize()} security challenge: Phone verification required (WorkOS Radar).")
                if code:
                    return code
                # Wait for intermediate redirect hops (e.g. WorkOS -> Authkit -> Cline -> localhost callback)
                hop = _wait_for(
                    lambda: _check_redirect(tab, redirect_uri),
                    timeout=10,
                    poll=0.5,
                )
                if hop:
                    return hop
                try:
                    u = tab.url
                    if "radar-challenge" in u:
                        raise Exception(f"{provider.capitalize()} security challenge: Phone verification required (WorkOS Radar).")
                    _raise_for_oauth_error(u)
                except Exception as e:
                    if "security challenge" in str(e) or "OAuth error" in str(e):
                        raise
                return None
            print(f"        [Step {step}] URL: {current_url[:80]}")

            # -------------------------------------------------------------
            # SPECIAL CASE 1: Workspace Terms of Service ("Welcome to your new account")
            # Click ONCE and wait for Google backend to finish provisioning.
            # -------------------------------------------------------------
            if ("workspacetermsofservice" in current_url or "speedbump" in current_url) and not tos_accepted:
                print("        >> Workspace Terms of Service detected")
                tos_clicked = tab.run_js("""
                    window.scrollTo(0, document.body.scrollHeight);
                    const candidates = [
                        '#gaplustosNext button',
                        '#gaplustosNext input[type="submit"]',
                        '#gaplustosNext input',
                        '#gaplustosNext',
                        'input[type="submit"]',
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            el.click();
                            return 'tos_sel:' + sel;
                        }
                    }
                    const btn = Array.from(document.querySelectorAll('button, input[type="submit"], div[role="button"]'))
                        .find(el => {
                            const t = (el.innerText || el.value || el.textContent || '').toLowerCase();
                            return t.includes('i understand') || t.includes('saya memahami') || t.includes('accept') || t.includes('saya setuju');
                        });
                    if (btn) {
                        btn.click();
                        return 'tos_text';
                    }
                    return null;
                """)

                if tos_clicked:
                    tos_accepted = True
                    print(f"        >> Accepted TOS ({tos_clicked}) — waiting for Google backend...")
                    # Event-driven: return as soon as the page leaves TOS or the code arrives.
                    done = _wait_for(
                        lambda: _check_redirect(tab, redirect_uri)
                        or (("workspacetermsofservice" not in _safe_url(tab)
                             and "speedbump" not in _safe_url(tab)) and "left-tos")
                        or None,
                        timeout=max(timing["after_tos_click"] * 2, 6),
                        poll=0.4,
                    )
                    if done and done != "left-tos":
                        return done
                    continue

            # If still on TOS after clicking, just wait for Google server without re-clicking
            if "workspacetermsofservice" in current_url or "speedbump" in current_url:
                print("        >> Waiting for Workspace TOS provisioning...")
                _nap(1.0, jitter=0.5)
                continue

            # -------------------------------------------------------------
            # SPECIAL CASE 2: Unknown Error page (Google glitch recovery)
            # -------------------------------------------------------------
            if "unknownerror" in current_url:
                print("        >> Google unknownerror page — clicking continue/retry...")
                tab.run_js("""
                    const btn = Array.from(document.querySelectorAll('button, a, input[type="submit"], div[role="button"]'))
                        .find(el => el.offsetParent !== null);
                    if (btn) btn.click();
                """)
                _wait_for_url_change(tab, current_url, timeout=4)
                continue

            # -------------------------------------------------------------
            # SPECIAL CASE 3: Account Chooser / Select Account page
            # -------------------------------------------------------------
            is_account_chooser = (
                "accountchooser" in current_url
                or "chooseaccount" in current_url
                or "selectaccount" in current_url
            )

            if is_account_chooser:
                print("        >> Account Chooser detected")
                account_clicked = tab.run_js("""
                    const targetEmail = (arguments[0] || '').toLowerCase().trim();
                    const username = targetEmail.split('@')[0];

                    const accountItems = Array.from(
                        document.querySelectorAll('li, div[role="link"], div[role="button"], div.J1DgEc, a')
                    ).filter(el => el.offsetParent !== null);

                    const found = accountItems.find(el => {
                        const t = (el.innerText || el.textContent || '').toLowerCase();
                        const isNotAnother = !t.includes('use another account') && !t.includes('gunakan akun lain');
                        const matches = t.includes(targetEmail) || (username.length > 4 && t.includes(username));
                        return isNotAnother && matches;
                    });

                    if (found) {
                        found.scrollIntoView();
                        found.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        found.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        found.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                        found.click();
                        return 'account_card:' + targetEmail;
                    }
                    return null;
                """, email or "")

                if account_clicked:
                    print(f"        >> Selected account ({account_clicked})")
                    _wait_for_url_change(tab, current_url, timeout=4)
                    # Check if password is requested again
                    try:
                        pwd_box = tab.ele('tag:input@@type=password', timeout=1.5)
                        if pwd_box and password:
                            print("        >> Password requested on account select, re-entering...")
                            _force_input(tab, pwd_box, password, timeout=5, desc="password re-entry")
                            _find_and_click(tab, ["#passwordNext", "tag:button@@text():Next", "tag:button@@text():Berikutnya"], timeout=1.5)
                            _wait_for_url_change(tab, tab.url, timeout=4)
                    except Exception:
                        pass
                    code = _check_redirect(tab, redirect_uri)
                    if code:
                        return code
                    continue

            # -------------------------------------------------------------
            # GENERAL CASE: Consent / Permissions / Submit Action Buttons
            # -------------------------------------------------------------
            before_click = _safe_url(tab)
            clicked = tab.run_js("""
                // 1. Auto-check any unchecked consent checkboxes
                document.querySelectorAll('input[type="checkbox"]:not(:checked)')
                    .forEach(cb => cb.click());

                // 2. High-priority ID selectors (Allow, Consent, Confirm, Approve)
                const prioritySelectors = [
                    '#submit_approve_access button',
                    '#submit_approve_access',
                    '#confirm',
                    '#next button',
                    '#next',
                    'input[type="submit"]',
                ];
                for (const sel of prioritySelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return 'id:' + sel;
                    }
                }

                // 3. Action text search across all buttons, links, and submit inputs
                const actionKeywords = [
                    // Primary consent actions
                    'allow', 'izinkan',
                    'continue', 'lanjutkan',
                    'sign in', 'masuk',
                    'accept', 'terima',
                    'i agree', 'saya setuju',
                    'confirm', 'konfirmasi',
                    'next', 'berikutnya',
                    'i understand', 'saya memahami', 'saya mengerti',
                    // Chrome browser profile / sync dismissals
                    'stay signed out', 'tetap logout', 'tetap keluar',
                    'no thanks', 'tidak, terima kasih', 'tidak terima kasih', 'lain kali',
                    'not now', 'jangan sekarang', 'nanti saja',
                    'dont turn on sync', "don't turn on sync", 'jangan aktifkan sinkronisasi',
                    'keep data separate', 'pisahkan data',
                ];

                const candidates = Array.from(
                    document.querySelectorAll('button, a, input[type="submit"], div[role="button"], span[role="button"]')
                ).filter(el => el.offsetParent !== null);

                for (const kw of actionKeywords) {
                    const found = candidates.find(el => {
                        const t = (el.innerText || el.value || el.textContent || '').toLowerCase().trim();
                        return t === kw || t.includes(kw);
                    });
                    if (found) {
                        found.click();
                        return 'action_text:' + kw;
                    }
                }

                return null;
            """)

            if clicked:
                print(f"        >> Clicked ({clicked})")
                # Event-driven: proceed as soon as navigation starts, then a
                # short human-like settle — much faster than a fixed sleep.
                _wait_for_url_change(tab, before_click, timeout=after_btn + 2)
                _nap(min(after_btn, 0.8), jitter=0.5)
                code = _check_redirect(tab, redirect_uri)
                if code:
                    return code
                continue

            # Fallback to DrissionPage locator search
            locators_to_try = [
                "#submit_approve_access",
                "tag:button@@text():Allow",
                "tag:button@@text():Izinkan",
                "tag:button@@text():Continue",
                "tag:button@@text():Lanjutkan",
                "tag:button@@text():Sign in",
                "tag:button@@text():Masuk",
                "tag:button@@text():I understand",
                "tag:button@@text():Saya memahami",
                "tag:button@@text():Accept",
                "tag:input@@type=submit",
            ]
            dp_clicked = _find_and_click(tab, locators_to_try, timeout=0.5)
            if dp_clicked:
                print("        >> Button clicked (via DrissionPage locator)")
                _wait_for_url_change(tab, before_click, timeout=after_btn + 2)
                _nap(min(after_btn, 0.8), jitter=0.5)
                code = _check_redirect(tab, redirect_uri)
                if code:
                    return code
                continue

            # No button found on this step — brief jittered wait
            _nap(timing["no_btn_wait"], jitter=0.5)

            code = _check_redirect(tab, redirect_uri)
            if code:
                return code

        except Exception as e:
            # Handle DrissionPage DOM context reload / page navigation in progress
            err_str = str(e)
            if "页面" in err_str or "refresh" in err_str.lower() or "context" in err_str.lower() or "lost" in err_str.lower() or "disconnected" in err_str.lower():
                _nap(0.5, jitter=0.5)
                code = _check_redirect(tab, redirect_uri)
                if code:
                    return code
                continue
            raise Exception(clean_exception(e))

    tail = (last_url or _safe_url(tab))[:120]
    raise Exception(
        f"Too many Google confirmation steps ({MAX_STEPS}x) — "
        f"possibly stuck on an unknown page (last: {tail or 'unknown'})"
    )


# Full fingerprint-spoofing payload injected into every new document BEFORE
# any page script runs (so Google's risk analysis never sees automation flags).
STEALTH_JS = """
(() => {
    // 1. WeakMap-based native function spoofing (Patchright & rebrowser standard)
    const nativeMap = new WeakMap();
    const origToString = Function.prototype.toString;

    function setNative(fn, name) {
        try {
            Object.defineProperty(fn, 'name', { value: name || fn.name || '', configurable: true });
        } catch(e) {}
        nativeMap.set(fn, name || fn.name || '');
        return fn;
    }

    try {
        Function.prototype.toString = function() {
            if (nativeMap.has(this)) {
                const name = nativeMap.get(this);
                return `function ${name}() { [native code] }`;
            }
            return origToString.call(this);
        };
        setNative(Function.prototype.toString, 'toString');
    } catch(e) {}

    // 2. navigator.webdriver -> MUST be absent from navigator instance and prototype
    // Sannysoft uses: navigator.webdriver || _.has(navigator, "webdriver")
    // If 'webdriver' exists as an own property, Lodash _.has() returns true!
    try {
        delete Navigator.prototype.webdriver;
    } catch(e) {}
    try {
        delete navigator.webdriver;
    } catch(e) {}

    // 3. window.chrome object
    try {
        window.chrome = {
            runtime: {},
            loadTimes: setNative(function() {}, 'loadTimes'),
            csi: setNative(function() {}, 'csi'),
            app: {}
        };
    } catch(e) {}

    // 4. Plugins (passes: instanceof PluginArray, length > 0, plugins[0].toString() === '[object Plugin]')
    try {
        const pluginsData = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
        ];
        const pArray = Object.create(PluginArray.prototype);
        pluginsData.forEach((p, idx) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperties(plugin, {
                name: { value: p.name, enumerable: true },
                filename: { value: p.filename, enumerable: true },
                description: { value: p.description, enumerable: true },
                length: { value: 0, enumerable: true }
            });
            Object.defineProperty(plugin, Symbol.toStringTag, { value: 'Plugin' });
            pArray[idx] = plugin;
            pArray[p.name] = plugin;
        });
        Object.defineProperties(pArray, {
            length: { value: pluginsData.length, enumerable: true },
            item: { value: setNative(function(i) { return this[i] || null; }, 'item') },
            namedItem: { value: setNative(function(n) { return this[n] || null; }, 'namedItem') },
            refresh: { value: setNative(function() {}, 'refresh') }
        });
        Object.defineProperty(pArray, Symbol.toStringTag, { value: 'PluginArray' });

        Object.defineProperty(navigator, 'plugins', {
            get: setNative(() => pArray, 'get plugins'),
            configurable: true,
            enumerable: true
        });
    } catch(e) {}
    // 5. Languages
    try {
        Object.defineProperty(navigator, 'languages', {
            get: setNative(() => ['en-US', 'en'], 'get languages'),
            configurable: true,
            enumerable: true
        });
    } catch(e) {}

    // 6. Permissions query
    try {
        const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = setNative((params) => {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission, onchange: null });
            }
            return origQuery(params);
        }, 'query');
    } catch(e) {}

    // 7. Console.enable Leak Mitigation (Patchright)
    // Netralkan getter-getter traps di arguments console
    try {
        for (const method of ['debug', 'log', 'info', 'warn', 'error', 'table', 'dir']) {
            if (console[method]) {
                const orig = console[method].bind(console);
                console[method] = setNative(function(...args) {
                    try { orig(...args); } catch(e) {}
                }, method);
            }
        }
    } catch(e) {}

    // 8. Hapus variable signature automation
    const leakPrefixes = ['$cdc_', '$chrome_', '__nightmare', '__selenium', '_Selenium_IDE_Recorder'];
    for (const k of Object.keys(window)) {
        if (leakPrefixes.some(p => k.startsWith(p))) {
            try { delete window[k]; } catch(e) {}
        }
    }

    // 9. Closed Shadow Root deep query support
    window.__deepQuerySelector = function(selector, root) {
        root = root || document;
        const el = root.querySelector(selector);
        if (el) return el;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let curr = walker.nextNode();
        while (curr) {
            if (curr.shadowRoot) {
                const found = window.__deepQuerySelector(selector, curr.shadowRoot);
                if (found) return found;
            }
            curr = walker.nextNode();
        }
        return null;
    };
})();
"""


def _apply_stealth(page):
    """Inject fingerprint spoofing before any page script runs (non-fatal)."""
    try:
        page.run_cdp(
            'Page.addScriptToEvaluateOnNewDocument',
            source=STEALTH_JS,
        )
    except Exception:
        # Non-critical — continue even if CDP injection fails
        pass


def _submit_login_step(tab, next_locators, timeout=1.5):
    """Click Next (or press Enter as fallback). Returns True on submit."""
    if _find_and_click(tab, next_locators, timeout=timeout):
        return True
    try:
        tab.actions.type(Keys.ENTER)
        return True
    except Exception:
        return False


def _submit_and_advance(tab, next_locators, timeout=1.5, desc="step"):
    """Submit a login step and VERIFY the page actually advanced.

    Google enables the Next button only after validating the field; a click
    that lands too early is silently swallowed (no navigation, no error).
    So: up to 3 attempts (click, ENTER, click) each followed by an advance
    wait. Raises a precise error only after all attempts stall.
    """
    def _advanced(before):
        return _wait_for(
            lambda: (
                ((_safe_url(tab).split("#")[0] != before.split("#")[0]) and _safe_url(tab))
                or _check_redirect(tab, None)
                or _page_has_text(tab, "welcome", "selamat datang", "verify it's you")
                or None
            ),
            timeout=5,
            poll=0.2,
        )

    before = _safe_url(tab)
    # Attempt 1: click Next.
    if not _submit_login_step(tab, next_locators, timeout=timeout):
        raise Exception(f"Next button ({desc}) not found")
    if _advanced(before):
        return True
    # Attempt 2: ENTER key (catches swallowed clicks).
    try:
        tab.actions.type(Keys.ENTER)
    except Exception:
        pass
    if _advanced(before):
        return True
    # Attempt 3: re-click (Google may have enabled the button late).
    _nap(0.8, jitter=0.3)
    _find_and_click(tab, next_locators, timeout=timeout)
    if _advanced(before):
        return True
    err = _check_google_errors(tab)
    if err:
        raise Exception(err)
    raise Exception(
        f"Page did not advance after submitting {desc} 3x — "
        "Google may be slow or showing an unknown page (retry the account)"
    )




def google_login(
    auth_url,
    email,
    password,
    timing,
    redirect_uri=DEFAULT_REDIRECT_URI,
    provider="antigravity",
    flow_type="code",
    device_code=None,
    code_verifier=None,
    router_api=None,
):
    """Login to Google OAuth via DrissionPage. Returns auth code (or conn_id for device_code flow).

    Creates a fresh ChromiumPage per account for full session isolation.
    Each page gets its own temp user data dir (cleaned up after).
    """
    from DrissionPage import ChromiumPage, ChromiumOptions

    tmp_dir = tempfile.mkdtemp(prefix="gs2r_")
    # Pre-configure profile preferences so Chrome never prompts to "Sign in to Chrome"
    # or "Turn on sync", and disables password save prompts.
    default_dir = os.path.join(tmp_dir, "Default")
    os.makedirs(default_dir, exist_ok=True)
    try:
        with open(os.path.join(default_dir, "Preferences"), "w", encoding="utf-8") as f:
            json.dump({
                "signin": {
                    "allowed": False,
                    "allowed_on_next_startup": False,
                },
                "sync": {
                    "has_setup_completed": False,
                },
                "credentials_enable_service": False,
                "profile": {
                    "password_manager_enabled": False,
                },
            }, f)
    except Exception:
        pass

    co = ChromiumOptions()
    for arg in CHROME_ARGS:
        co.set_argument(arg)
    co.set_user_data_path(tmp_dir)
    co.set_local_port(random.randint(19200, 29200))

    page = ChromiumPage(co)

    # Anti-detection: spoof automation fingerprint in every new document
    # BEFORE navigation (webdriver, plugins, languages, permissions, cdc vars).
    _apply_stealth(page)

    try:
        print(f"  [Google] Navigate...")
        page.get(auth_url)

        # Event-driven initial wait: proceed as soon as the email field is
        # usable, the flow auto-redirected, or Google shows an error — no
        # fixed sleep.
        code = _check_redirect(page, redirect_uri)
        if code:
            print(f"  [Google] OK auth code (auto-redirect)")
            return code
        err = _check_google_errors(page)
        if err:
            raise Exception(err)
        if not _wait_for_email_field(page, max_timeout=8):
            # No email field: maybe already past identifier (session/password
            # page), auto-redirected, or provider interstitial. Re-check.
            code = _check_redirect(page, redirect_uri)
            if code:
                print(f"  [Google] OK auth code (auto-redirect)")
                return code
            _raise_for_oauth_error(_safe_url(page))
            err = _check_google_errors(page)
            if err:
                raise Exception(err)

        # Check if landing page is Cline AuthKit / provider selection
        current_url = _safe_url(page)

        if "kilo.ai" in current_url or provider == "kilocode":
            print("  [Google] Kilo Code detected — initiating Google OAuth via UI...")
            # Step A: If on email entry screen, click "Back to sign in options"
            _nap(0.5, jitter=0.5)
            for _ in range(3):
                if _click_first_button_by_text(page, "Back to sign in options"):
                    _nap(0.5, jitter=0.5)
                    break

            # Step B: Click "Continue with Google" button (one CDP round-trip)
            if not _click_first_button_by_text(page, "Continue with Google", "Google"):
                for b in page.eles("tag:button"):
                    if "Google" in (b.text or ""):
                        try:
                            b.click(by_js=True)
                            break
                        except Exception:
                            pass

            # Step C: Wait for Turnstile verification & navigation to Google login
            if not _wait_for_url_contains(page, "accounts.google.com", timeout=20):
                _raise_for_oauth_error(_safe_url(page))
                raise Exception("Kilo Code: never reached Google login (Turnstile/redirect stuck)")
        elif "authkit.cline.bot" in current_url or "cline.bot" in current_url or _page_has_text(page, "Continue with Google"):
            print("  [Google] Cline AuthKit detected — clicking 'Continue with Google'...")
            clicked = _find_and_click(page, [
                "tag:a@@href:provider=GoogleOAuth",
                "div[data-method='google'] a",
                "tag:button@@text():Continue with Google",
                "tag:a@@text():Continue with Google",
                "text():Continue with Google",
            ], timeout=3)
            if not clicked:
                # Fallback JS: click button/link with Google text or provider=GoogleOAuth
                page.run_js("""
                    const el = Array.from(document.querySelectorAll('a, button, div[role="button"]'))
                        .find(e => (e.innerText || e.textContent || '').includes('Continue with Google') ||
                                   (e.href && e.href.includes('provider=GoogleOAuth')));
                    if (el) el.click();
                """)

            # Wait for Google login page to load
            for _ in range(20):
                time.sleep(0.5)
                code = _check_redirect(page, redirect_uri)
                if code:
                    print(f"  [Google] OK auth code (auto-redirect)")
                    return code
                try:
                    if "accounts.google.com" in page.url:
                        break
                except Exception:
                    pass
        # -------------------------------------------------------------
        # STEP 1: Email Input & Submit (skip if already past identifier)
        # -------------------------------------------------------------
        pwd_already = False
        try:
            pwd_already = bool(page.run_js(
                "const el = document.querySelector('input[type=\"password\"]');"
                " return (el && el.offsetParent !== null) ? true : false;"
            ))
        except Exception:
            pass
        if not pwd_already:
            print(f"  [Google] Email...")
            _force_input(page, "#identifierId", email, timeout=15, desc="email field")
            _nap(timing["after_email_input"], jitter=0.5)

            # Submit email: Click Next (verified — retries if click swallowed).
            _submit_and_advance(page, [
                "#identifierNext",
                "tag:button@@text():Next",
                "tag:button@@text():Berikutnya",
                "tag:button@@text():Lanjutkan",
            ], desc="email")
        else:
            print(f"  [Google] Email... (skipped, already on password page)")

        # -------------------------------------------------------------
        # STEP 2: Wait & Input Password
        # -------------------------------------------------------------
        print(f"  [Google] Waiting for password page...")
        # Fast path: code may already be available (auto-advanced to consent).
        code = _check_redirect(page, redirect_uri)
        if code:
            print(f"  [Google] OK auth code (auto-redirect)")
            return code
        pwd_input = _wait_for_password_field(page, max_timeout=timing["password_timeout"])
        if not pwd_input:
            # Navigated past sign-in without a password prompt — check code/error.
            code = _check_redirect(page, redirect_uri)
            if code:
                print(f"  [Google] OK auth code (auto-redirect)")
                return code
            _raise_for_oauth_error(_safe_url(page))
            raise Exception("Password field did not appear within timeout")

        print(f"  [Google] Password...")
        _force_input(page, pwd_input, password, timeout=5, desc="password field")
        _nap(timing["after_pw_input"], jitter=0.5)

        # Submit password: Click Next (verified — retries if click swallowed).
        _submit_and_advance(page, [
            "#passwordNext",
            "tag:button@@text():Next",
            "tag:button@@text():Berikutnya",
            "tag:button@@text():Lanjutkan",
        ], desc="password")

        # -------------------------------------------------------------
        # STEP 3: Handle Consent & OAuth Redirection
        # -------------------------------------------------------------
        print(f"  [Google] Consent...")
        code = _handle_consent_loop(
            page,
            timing,
            redirect_uri=redirect_uri,
            email=email,
            password=password,
            flow_type=flow_type,
            provider=provider,
        )

        if flow_type == "device_code":
            print(f"  [Google] Returning from Google OAuth to {provider}...")
            # Wait for page to navigate to provider device-auth page
            _wait_for_url_contains(page, ("device-auth", "account-verification", "kilo.ai"), timeout=15)

            _nap(0.8, jitter=0.5)
            # Click Authorize/Approve button on device page (fast JS path first)
            authorized = bool(_click_first_button_by_text(
                page, "authorize", "approve", "allow", "confirm", "izinkan"
            ))
            if not authorized:
                for _ in range(6):
                    try:
                        hit = False
                        for b in page.eles("tag:button"):
                            t = (b.text or "").strip().lower()
                            if t in ("authorize", "approve", "allow", "confirm", "izinkan") or "authorize" in t:
                                print(f"        >> Found '{b.text}' button, clicking...")
                                b.click(by_js=True)
                                authorized = True
                                hit = True
                                break
                        if hit:
                            break
                    except Exception:
                        pass
                    time.sleep(0.5)

            # Execute approval API inside page context as fallback/guarantee.
            # device_code passed as a script argument (never interpolated).
            if provider == "kilocode" and device_code:
                try:
                    page.run_js("""
                        (async () => {
                            try {
                                await fetch('/api/device-auth/tokens', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ code: arguments[0] })
                                });
                            } catch (e) {}
                        })();
                    """, device_code)
                except Exception:
                    pass

            if router_api and device_code:
                print(f"  [API] Polling 9Router for {provider} connection...")
                poll_result = router_api.poll_until_complete(
                    device_code=device_code,
                    code_verifier=code_verifier,
                    provider=provider,
                    interval=3,
                    timeout=timing.get("redirect_wait", 30) + 20,
                )
                conn_id = (poll_result.get("connection", {}) or {}).get("id", "OK")
                return conn_id
            return "OK"
        if code:
            print(f"  [Google] OK auth code obtained")
            return code

        # Final wait & redirect check
        final = _wait_for(
            lambda: _check_redirect(page, redirect_uri),
            timeout=timing["redirect_wait"],
            poll=0.3,
        )
        if final:
            print(f"  [Google] OK auth code obtained")
            return final
        try:
            _raise_for_oauth_error(_safe_url(page))
        except Exception as e:
            if "OAuth error" in str(e):
                raise

        raise Exception("Auth code not captured")
    except Exception as e:
        raise Exception(clean_exception(e))

    finally:
        try:
            page.quit()
        except Exception:
            pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def kill_zombie_browsers():
    """Kill leftover Chrome processes from previous runs."""
    import os
    import glob
    import subprocess

    if os.name == "nt":
        # Windows: find chrome.exe processes launched by us (gs2r_ profile
        # in the command line) via CIM, then taskkill them. Best-effort.
        try:
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
                    "| Where-Object { $_.CommandLine -like '*gs2r_*' } "
                    "| Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True, text=True, timeout=20,
            )
            for line in (out.stdout or "").splitlines():
                pid = line.strip()
                if pid.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=10,
                    )
        except Exception:
            pass
        return

    try:
        subprocess.run(
            ["pkill", "-f", "chrome.*gs2r_"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    for old_tmp in glob.glob(os.path.join(tempfile.gettempdir(), "gs2r_*")):
        try:
            shutil.rmtree(old_tmp, ignore_errors=True)
        except Exception:
            pass
