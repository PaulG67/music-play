"""OIDC login mode and internal-network detection."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(tempfile.mkdtemp(prefix="music-play-oidc-"))
for _name, _subdir in (
    ("CONFIG_DIR", "config"),
    ("CACHE_DIR", "cache"),
    ("LOG_DIR", "logs"),
):
    _path = _ROOT / _subdir
    _path.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_path)

from app.oidc import config as oidc_config
from app.oidc import flow as oidc_flow


class FakeRequest:
    def __init__(self, host: str, forwarded: str | None = None):
        self.client = MagicMock(host=host)
        self.headers = {}
        if forwarded:
            self.headers["x-forwarded-for"] = forwarded
        self.base_url = "https://music.example.com/"


class OidcAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        oidc_config._cache = None
        if oidc_config.CONFIG_FILE.exists():
            oidc_config.CONFIG_FILE.unlink()

    def test_internal_ip_detection(self) -> None:
        request = FakeRequest("8.8.8.8", forwarded="192.168.1.50")
        self.assertTrue(
            oidc_flow.client_is_internal(
                request,
                "10.0.0.0/8,192.168.0.0/16",
            )
        )
        external = FakeRequest("8.8.8.8")
        self.assertFalse(
            oidc_flow.client_is_internal(
                external,
                "10.0.0.0/8,192.168.0.0/16",
            )
        )

    def test_internal_both_mode(self) -> None:
        oidc_config.save(
            {
                "enabled": True,
                "issuer": "https://id.example.com",
                "client_id": "music-play",
                "client_secret": "secret",
                "auth_mode": "internal_both",
                "display_name": "Pocket ID",
                "internal_networks": "192.168.0.0/16",
            }
        )
        with patch("app.oidc.flow.auth_store.needs_setup", return_value=False), patch(
            "app.oidc.flow.auth_store.login_required", return_value=True
        ):
            internal = oidc_flow.login_methods(FakeRequest("192.168.0.10"))
            external = oidc_flow.login_methods(FakeRequest("203.0.113.10"))
        self.assertTrue(internal["passwordLogin"])
        self.assertTrue(internal["oidcLogin"])
        self.assertTrue(internal["internalAccess"])
        self.assertFalse(external["passwordLogin"])
        self.assertTrue(external["oidcLogin"])
        self.assertEqual(internal["oidcLabel"], "Pocket ID")

    def test_both_mode_always_offers_password(self) -> None:
        oidc_config.save(
            {
                "enabled": True,
                "issuer": "https://id.example.com",
                "client_id": "music-play",
                "client_secret": "secret",
                "auth_mode": "both",
            }
        )
        with patch("app.oidc.flow.auth_store.needs_setup", return_value=False), patch(
            "app.oidc.flow.auth_store.login_required", return_value=True
        ):
            methods = oidc_flow.login_methods(FakeRequest("203.0.113.10"))
        self.assertTrue(methods["passwordLogin"])
        self.assertTrue(methods["oidcLogin"])


if __name__ == "__main__":
    unittest.main()
