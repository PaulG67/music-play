"""Tolerant resolution of library paths under /music."""

from __future__ import annotations

import tempfile
import unittest
import unicodedata
from pathlib import Path

from app.navidrome.paths import resolve_library_rel_path, try_resolve_library_rel_path


class LibraryPathResolveTests(unittest.TestCase):
    def test_exact_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = (
                root
                / "David Guetta"
                / "Nothing but the Beat (Ultimate Edition)"
                / "01-01 - Titanium (feat. Sia).mp3"
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")

            found = resolve_library_rel_path(
                root,
                "David Guetta/Nothing but the Beat (Ultimate Edition)/"
                "01-01 - Titanium (feat. Sia).mp3",
            )
            self.assertEqual(found.resolve(), target.resolve())

    def test_case_insensitive_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "David Guetta" / "Album" / "Titanium.mp3"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")

            found = resolve_library_rel_path(
                root,
                "david guetta/album/titanium.mp3",
            )
            self.assertEqual(found.resolve(), target.resolve())

    def test_unicode_nfd_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artist = unicodedata.normalize("NFD", "Beyoncé")
            target = root / artist / "Halo.mp3"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")

            found = resolve_library_rel_path(
                root,
                unicodedata.normalize("NFC", "Beyoncé/Halo.mp3"),
            )
            self.assertEqual(found.resolve(), target.resolve())

    def test_fuzzy_filename_without_track_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = (
                root
                / "David Guetta"
                / "Nothing but the Beat (Ultimate Edition)"
                / "Titanium (feat. Sia).mp3"
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")

            found = resolve_library_rel_path(
                root,
                "David Guetta/Nothing but the Beat (Ultimate Edition)/"
                "01-01 - Titanium (feat. Sia).mp3",
            )
            self.assertEqual(found.resolve(), target.resolve())

    def test_fuzzy_find_under_artist_when_album_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = (
                root
                / "David Guetta"
                / "Nothing But The Beat"
                / "Titanium (feat Sia).mp3"
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")

            found = resolve_library_rel_path(
                root,
                "David Guetta/Nothing but the Beat (Ultimate Edition)/"
                "01-01 - Titanium (feat. Sia).mp3",
            )
            self.assertEqual(found.resolve(), target.resolve())

    def test_missing_still_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Other").mkdir()
            with self.assertRaises(RuntimeError) as ctx:
                resolve_library_rel_path(
                    root,
                    "David Guetta/Missing/01-01 - Titanium.mp3",
                )
            self.assertIn("Datei nicht gefunden", str(ctx.exception))
            self.assertIsNone(
                try_resolve_library_rel_path(
                    root,
                    "David Guetta/Missing/01-01 - Titanium.mp3",
                )
            )

    def test_try_resolve_returns_none_for_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(try_resolve_library_rel_path(Path(tmp), ""))
            self.assertIsNone(try_resolve_library_rel_path(Path(tmp), "../x.mp3"))


if __name__ == "__main__":
    unittest.main()
