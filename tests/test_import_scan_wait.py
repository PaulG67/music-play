"""Navidrome import should not burn ~90s waiting for watcher scans."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(tempfile.mkdtemp(prefix="music-play-import-test-"))
for _name, _subdir in (
    ("CONFIG_DIR", "config"),
    ("CACHE_DIR", "cache"),
    ("LOG_DIR", "logs"),
):
    _path = _ROOT / _subdir
    _path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(_name, str(_path))

from app.navidrome import importer


class FakeClient:
    def __init__(self, statuses: list[dict]):
        self.statuses = list(statuses)
        self.started = 0
        self.polls = 0

    def start_scan(self) -> None:
        self.started += 1

    def scan_status(self) -> dict:
        self.polls += 1
        if self.statuses:
            return self.statuses.pop(0)
        return {"scanning": False, "count": 0}


class ImportScanWaitTests(unittest.TestCase):
    def test_watcher_never_scanning_exits_quickly(self) -> None:
        client = FakeClient([{"scanning": False}] * 10)
        started = time.monotonic()
        with patch.object(importer.time, "sleep", return_value=None):
            importer._wait_for_scan(client, timeout=90)
        elapsed = time.monotonic() - started
        self.assertEqual(client.started, 1)
        self.assertLessEqual(client.polls, 4)
        self.assertLess(elapsed, 1.0)

    def test_returns_when_scan_finishes(self) -> None:
        client = FakeClient(
            [
                {"scanning": True},
                {"scanning": True},
                {"scanning": False},
            ]
        )
        with patch.object(importer.time, "sleep", return_value=None):
            importer._wait_for_scan(client, timeout=90)
        self.assertEqual(client.started, 1)
        self.assertEqual(client.polls, 3)

    def test_await_indexed_uses_live_search_before_full_refresh(self) -> None:
        track = {"title": "Titanium", "artist": "David Guetta"}
        hit = {"id": "abc", "title": "Titanium", "artist": "David Guetta"}
        client = FakeClient([{"scanning": False}] * 5)
        live_calls = {"n": 0}

        def fake_locate(payload, *, force_refresh: bool = False):
            if force_refresh:
                self.fail("full index refresh should not run once live search hits")
            live_calls["n"] += 1
            if live_calls["n"] >= 2:
                return hit
            return None

        clock = {"t": 100.0}

        def fake_time() -> float:
            return clock["t"]

        def fake_sleep(seconds: float) -> None:
            clock["t"] += float(seconds)

        with (
            patch.object(importer, "_locate", side_effect=fake_locate),
            patch.object(importer, "_wait_for_scan", return_value=None),
            patch.object(importer, "_refresh_index_async", return_value=None),
            patch.object(importer.time, "sleep", side_effect=fake_sleep),
            patch.object(importer.time, "time", side_effect=fake_time),
        ):
            found = importer._await_indexed(track, client, timeout=40)

        self.assertEqual(found, hit)
        self.assertGreaterEqual(live_calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
