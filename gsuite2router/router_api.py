"""9Router REST API client — login, OAuth, provider management.

Single persistent HTTP(S) connection (keep-alive) per instance so repeated
authorize/exchange/poll calls skip TCP+TLS handshakes (~200-400ms saved per
call). Thread-safe via a lock; auto-reconnects on drops.
"""

import json
import ssl
import time
import random
import threading
import http.client
import urllib.parse


class RouterAPI:
    """HTTP client for 9Router REST API (persistent keep-alive connection)."""

    def __init__(self, base_url, password, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._cookie = None
        self._lock = threading.RLock()
        self._conn = None
        parts = urllib.parse.urlsplit(self.base_url)
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._ctx = ctx
        self._ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

    def _connect(self):
        """(Re)create the underlying connection. Caller MUST hold the lock."""
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
        finally:
            self._conn = None
        if self._scheme == "https":
            self._conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout, context=self._ctx
            )
        else:
            self._conn = http.client.HTTPConnection(
                self._host, self._port, timeout=self.timeout
            )
        try:
            self._conn.connect()
        except Exception:
            pass

    def _request(self, method, path, body=None, _retries=2):
        """Make HTTP request, return (status, data, set_cookie_headers)."""
        payload = json.dumps(body).encode() if body is not None else None
        target = self._prefix + path
        last_err = None
        for attempt in range(_retries + 1):
            with self._lock:
                if self._conn is None:
                    self._connect()
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": self._ua,
                    "Accept": "application/json, text/plain, */*",
                    "Connection": "keep-alive",
                }
                if self._cookie:
                    headers["Cookie"] = self._cookie
                if payload is not None:
                    headers["Content-Length"] = str(len(payload))
                try:
                    self._conn.request(method, target, body=payload, headers=headers)
                    resp = self._conn.getresponse()
                    status = resp.status
                    raw = resp.read().decode("utf-8", "replace")
                    set_cookie = [
                        v for k, v in resp.getheaders() if k.lower() == "set-cookie"
                    ]
                    if resp.getheader("Connection", "").lower() == "close":
                        try:
                            self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                except (http.client.HTTPException, OSError, ssl.SSLError) as e:
                    last_err = e
                    self._conn = None
                    if attempt < _retries:
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    raise Exception(f"Request error: {e}")
            try:
                parsed = json.loads(raw) if raw else None
            except (json.JSONDecodeError, ValueError):
                parsed = raw
            if status in (502, 503, 504) and attempt < _retries:
                time.sleep(0.5 * (attempt + 1) + random.uniform(0, 0.2))
                continue
            return status, parsed, set_cookie
        raise Exception(f"Request error: {last_err}")

    def close(self):
        """Close the persistent connection."""
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            finally:
                self._conn = None

    @staticmethod
    def _extract_auth_cookie(set_cookie_headers):
        """Extract auth_token from Set-Cookie headers."""
        for header in set_cookie_headers or []:
            for part in header.split(";"):
                part = part.strip()
                if part.startswith("auth_token="):
                    return f"auth_token={part.split('=', 1)[1]}"
        return None

    def login(self):
        """Login to 9Router with password, store auth cookie."""
        print("[9Router] Login...")
        status, data, cookies = self._request(
            "POST", "/api/auth/login", {"password": self.password}
        )
        if status != 200:
            raise Exception(f"Login failed ({status}): {data}")

        if isinstance(data, dict) and not data.get("success", True):
            raise Exception(f"Login failed: {data}")

        cookie = self._extract_auth_cookie(cookies)
        if not cookie:
            raise Exception("auth_token cookie not found in response")

        self._cookie = cookie
        print("[9Router] OK login successful")
        return cookie

    def start_oauth(self, redirect_uri, provider="antigravity"):
        """Start OAuth flow — returns authUrl, codeVerifier, state, and flowType."""
        if provider == "kilocode":
            status, data, _ = self._request("GET", f"/api/oauth/{provider}/device-code")
            if status != 200:
                raise Exception(f"Start OAuth ({provider}) failed ({status}): {data}")
            data = data or {}
            auth_url = data.get("verification_uri_complete") or data.get("verification_uri")
            if not auth_url:
                raise Exception(f"Incomplete device code response for {provider}: {data}")
            return {
                "authUrl": auth_url,
                "codeVerifier": data.get("codeVerifier"),
                "state": None,
                "flowType": "device_code",
                "deviceCode": data.get("device_code"),
                "interval": data.get("interval", 3),
                "expiresIn": data.get("expires_in", 300),
            }

        path = f"/api/oauth/{provider}/authorize?" + urllib.parse.urlencode(
            {"redirect_uri": redirect_uri}
        )
        status, data, _ = self._request("GET", path)

        if status != 200:
            raise Exception(f"Start OAuth ({provider}) failed ({status}): {data}")

        data = data or {}
        auth_url = data.get("authUrl")
        if not auth_url:
            raise Exception(f"Incomplete OAuth response for {provider}: {data}")

        return {
            "authUrl": auth_url,
            "codeVerifier": data.get("codeVerifier"),
            "state": data.get("state"),
            "flowType": data.get("flowType", "code"),
        }

    def poll_device_code(self, device_code, code_verifier=None, extra_data=None, provider="kilocode"):
        """Poll for token on device_code flow."""
        payload = {"deviceCode": device_code}
        if code_verifier is not None:
            payload["codeVerifier"] = code_verifier
        if extra_data is not None:
            payload["extraData"] = extra_data
        status, data, _ = self._request("POST", f"/api/oauth/{provider}/poll", payload)
        if status != 200:
            raise Exception(f"Poll device code ({provider}) failed ({status}): {data}")
        return data

    def poll_until_complete(self, device_code, code_verifier=None, extra_data=None, interval=3, timeout=60, provider="kilocode"):
        """Poll device code until connection is created or timeout.

        Polls immediately on entry (no leading sleep) and honors the server's
        ``interval`` hint, including ``slow_down`` backoff.
        """
        deadline = time.time() + timeout
        wait = max(0.5, min(interval, 5))
        while True:
            try:
                data = self.poll_device_code(device_code, code_verifier, extra_data, provider=provider)
            except Exception:
                data = None
            if isinstance(data, dict):
                if data.get("success"):
                    return data
                err = data.get("error")
                if err in ("expired_token", "access_denied"):
                    raise Exception(f"Authorization {err}: {data.get('errorDescription', '')}")
                if err == "slow_down":
                    wait = min(wait + 5, 30)
                elif err and err != "authorization_pending":
                    raise Exception(f"Authorization {err}: {data.get('errorDescription', '')}")
                hint = data.get("interval")
                if isinstance(hint, (int, float)) and hint > 0:
                    wait = max(1.0, min(hint, 10))
            if time.time() >= deadline:
                break
            time.sleep(wait + random.uniform(0, 0.4))
        raise Exception(f"Polling timeout waiting for {provider} authorization")

    def exchange_token(self, redirect_uri, code, code_verifier, state, provider="antigravity"):
        """Exchange OAuth auth code for connection (retries transient 5xx)."""
        payload = {
            "code": code,
            "redirectUri": redirect_uri,
        }
        if code_verifier is not None:
            payload["codeVerifier"] = code_verifier
        if state is not None:
            payload["state"] = state

        status, data, _ = self._request(
            "POST",
            f"/api/oauth/{provider}/exchange",
            payload,
        )

        if status not in (200, 201):
            raise Exception(f"Exchange token ({provider}) failed ({status}): {data}")

        return data

    def get_providers(self):
        """Get all provider connections."""
        status, data, _ = self._request("GET", "/api/providers")
        if status != 200:
            raise Exception(f"Get providers failed ({status}): {data}")
        if isinstance(data, dict):
            return data.get("connections", data.get("data", [])) or []
        return data or []

    def get_usage(self, connection_id):
        """Get usage/quota for a connection (None on transient failure)."""
        try:
            status, data, _ = self._request("GET", f"/api/usage/{connection_id}")
        except Exception:
            return None
        if status != 200:
            return None
        return data

    def delete_provider(self, provider_id):
        """Delete a provider connection."""
        try:
            status, _, _ = self._request("DELETE", f"/api/providers/{provider_id}")
        except Exception:
            return False
        return status in (200, 204)

    def reset_provider(self, provider_id):
        """Reset provider status (clear error, set active)."""
        try:
            self._request(
                "PUT",
                f"/api/providers/{provider_id}",
                {
                    "testStatus": "active",
                    "lastError": None,
                    "lastErrorAt": None,
                    "errorCode": None,
                    "backoffLevel": 0,
                },
            )
        except Exception:
            pass

    def test_provider(self, provider_id):
        """Test/re-verify a provider connection."""
        try:
            status, data, _ = self._request(
                "POST", f"/api/providers/{provider_id}/test"
            )
        except Exception:
            return False, "?"
        if status == 200 and isinstance(data, dict):
            return data.get("valid", False), data.get("testStatus", "?")
        return False, "?"
