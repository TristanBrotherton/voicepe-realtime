"""Device WebSocket authentication with a migration path.

The device link carries live microphone audio and can drive every exposed
Home Assistant tool, and the add-on listens on the host network. Anyone on
the LAN who can open that socket can spend OpenAI credit and act in the home,
so connections can be restricted two ways:

* a shared device token (``device_token``), presented by the firmware as an
  ``Authorization: Bearer <token>`` header (``?token=`` in the URL also
  works, for clients that cannot set headers);
* a source-address allowlist (``device_allowlist``: IPs/CIDRs, comma
  separated) — defense in depth that needs no firmware change.

``device_auth`` selects the policy:

* ``auto`` (default): ``enforce`` when a token is configured, else ``off``.
* ``enforce``: a valid token is required.
* ``permissive``: connections without a valid token are accepted but logged —
  use it while reflashing devices with the token, then switch to enforce.
* ``off``: no token check (the legacy behaviour; logged loudly at startup).

The allowlist, when set, applies in every mode (including ``off``):
configuring one is an unambiguous request.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

MODES = ("auto", "enforce", "permissive", "off")


@dataclass
class AuthDecision:
    allowed: bool
    authenticated: bool
    reason: str


def _parse_networks(raw: Iterable[str]) -> List[ipaddress._BaseNetwork]:
    networks = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning(f"⚠️ ignoring invalid device_allowlist entry {item!r}")
    return networks


class DeviceAuth:
    def __init__(self, token: str = "", mode: str = "auto", allowlist: Iterable[str] = ()):
        self.token = (token or "").strip()
        mode = (mode or "auto").strip().lower()
        if mode not in MODES:
            logger.warning(f"⚠️ unknown device_auth {mode!r}; using auto")
            mode = "auto"
        if mode == "auto":
            mode = "enforce" if self.token else "off"
        if mode in ("enforce", "permissive") and not self.token:
            logger.warning(f"⚠️ device_auth={mode} needs device_token; falling back to off")
            mode = "off"
        self.mode = mode
        self.networks = _parse_networks(allowlist)

    @classmethod
    def from_env(cls) -> "DeviceAuth":
        return cls(
            token=os.environ.get("DEVICE_TOKEN", ""),
            mode=os.environ.get("DEVICE_AUTH", "auto"),
            allowlist=os.environ.get("DEVICE_ALLOWLIST", "").split(","),
        )

    def describe(self) -> str:
        parts = [f"token={self.mode}"]
        if self.networks:
            parts.append(f"allowlist={len(self.networks)} network(s)")
        return ", ".join(parts)

    def log_startup(self) -> None:
        if self.mode == "off" and not self.networks:
            logger.warning(
                "⚠️ device WebSocket is UNAUTHENTICATED: any host on the network can connect, "
                "use OpenAI credit and drive exposed Home Assistant tools. Set device_token "
                "(and the same token in the firmware), or device_allowlist."
            )
        else:
            logger.info(f"🔐 device WebSocket access control: {self.describe()}")

    @staticmethod
    def _presented_token(websocket) -> str:
        headers = getattr(websocket, "headers", None) or {}
        try:
            auth = headers.get("authorization") or headers.get("Authorization") or ""
        except Exception:
            auth = ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        try:
            query = websocket.url.query if getattr(websocket, "url", None) else ""
            values = parse_qs(query).get("token") or []
            return values[0].strip() if values else ""
        except Exception:
            return ""

    def _client_ip(self, websocket) -> Optional[str]:
        client = getattr(websocket, "client", None)
        return getattr(client, "host", None) if client else None

    def check(self, websocket) -> AuthDecision:
        if self.networks:
            host = self._client_ip(websocket)
            try:
                address = ipaddress.ip_address(host) if host else None
            except ValueError:
                address = None
            if address is None or not any(address in net for net in self.networks):
                return AuthDecision(False, False, f"source {host or 'unknown'} not in device_allowlist")
        if self.mode == "off":
            return AuthDecision(True, False, "unauthenticated (device_auth off)")
        presented = self._presented_token(websocket)
        valid = bool(presented) and hmac.compare_digest(presented, self.token)
        if valid:
            return AuthDecision(True, True, "token ok")
        reason = "missing device token" if not presented else "invalid device token"
        if self.mode == "permissive":
            return AuthDecision(True, False, f"{reason} (permissive mode)")
        return AuthDecision(False, False, reason)

    def authorizes_request(self, authorization: str, extra_tokens: Iterable[str] = ()) -> bool:
        """True when an HTTP Authorization header carries a known token."""
        if not authorization or not authorization.lower().startswith("bearer "):
            return False
        presented = authorization[7:].strip()
        for token in [self.token, *extra_tokens]:
            if token and hmac.compare_digest(presented, token):
                return True
        return False
