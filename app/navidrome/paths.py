"""Tolerant resolution of audio paths under the shared music library."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

AUDIO_SUFFIXES = {
    ".m4a",
    ".mp4",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wav",
    ".wma",
    ".aiff",
    ".aif",
    ".ape",
    ".alac",
    ".webm",
}


def normalize_rel_path(rel_path: str | None) -> str | None:
    """Normalize a library-relative path. Returns None if invalid."""

    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    return rel


def try_resolve_library_rel_path(root: Path, rel_path: str | None) -> Path | None:
    """Like resolve_library_rel_path, but returns None instead of raising."""

    try:
        return resolve_library_rel_path(root, rel_path)
    except (RuntimeError, OSError, ValueError):
        return None


def resolve_library_rel_path(root: Path, rel_path: str | None) -> Path:
    """
    Resolve a relative path under the music share to a real file.

    Tolerant of case, Unicode NFC/NFD, track-number prefixes, and slight
    album/filename differences.
    """

    root = root.resolve()
    rel = normalize_rel_path(rel_path)
    if not rel:
        raise RuntimeError("Ungültiger Dateipfad.")

    exact = root / rel
    if exact.is_file():
        return _ensure_under_root(exact.resolve(), root)

    for form in ("NFC", "NFD"):
        norm = unicodedata.normalize(form, rel)
        candidate = root / norm
        if candidate.is_file():
            return _ensure_under_root(candidate.resolve(), root)

    insensitive = _case_insensitive_resolve(root, rel)
    if insensitive is not None:
        return insensitive

    fuzzy = _fuzzy_resolve_under_root(root, rel)
    if fuzzy is not None:
        return fuzzy

    raise RuntimeError(
        f"Datei nicht gefunden: {rel}. "
        "Pfad unter der Musikfreigabe prüfen "
        "(Groß/Klein, Albumordner, Dateiname)."
    )


def _ensure_under_root(resolved: Path, root: Path) -> Path:
    try:
        resolved.relative_to(root.resolve())
    except ValueError as ex:
        raise RuntimeError(
            f"Pfad liegt außerhalb der Musikfreigabe: {resolved}"
        ) from ex
    return resolved


def _case_insensitive_resolve(root: Path, rel: str) -> Path | None:
    current = root
    parts = Path(rel).parts
    for index, part in enumerate(parts):
        if not current.is_dir():
            return None
        try:
            children = list(current.iterdir())
        except OSError:
            return None

        by_name = {child.name.casefold(): child for child in children}
        match = by_name.get(part.casefold())
        if match is not None:
            current = match
            continue

        if index == len(parts) - 1:
            return _fuzzy_file_in_dir(current, part)

        folder = _fuzzy_dir_in_dir(current, part)
        if folder is None:
            return None
        current = folder

    if current.is_file():
        return _ensure_under_root(current.resolve(), root)
    return None


def _fuzzy_resolve_under_root(root: Path, rel: str) -> Path | None:
    """Search under the artist folder when album/filename layout differs."""

    parts = Path(rel).parts
    if len(parts) < 2:
        return _fuzzy_file_in_dir(root, parts[-1]) if parts else None

    artist_part = parts[0]
    filename = parts[-1]

    artist_dir = None
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name.casefold() == artist_part.casefold():
                artist_dir = child
                break
            if _name_similarity(child.name, artist_part) >= 90:
                artist_dir = child
                break
    except OSError:
        return None

    if artist_dir is None:
        return None

    if len(parts) >= 3:
        album_part = parts[1]
        album_dir = _fuzzy_dir_in_dir(artist_dir, album_part)
        if album_dir is not None:
            found = _fuzzy_file_in_dir(album_dir, filename)
            if found is not None:
                return found

    best: Path | None = None
    best_score = 0
    try:
        for path in artist_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.casefold() not in AUDIO_SUFFIXES:
                continue
            score = _filename_match_score(path.name, filename)
            if score > best_score:
                best_score = score
                best = path
    except OSError:
        return None

    if best is not None and best_score >= 82:
        return _ensure_under_root(best.resolve(), root)
    return None


def _fuzzy_dir_in_dir(parent: Path, wanted: str) -> Path | None:
    best: Path | None = None
    best_score = 0
    try:
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            score = _name_similarity(child.name, wanted)
            if score > best_score:
                best_score = score
                best = child
    except OSError:
        return None
    if best is not None and best_score >= 86:
        return best
    return None


def _fuzzy_file_in_dir(parent: Path, wanted_name: str) -> Path | None:
    best: Path | None = None
    best_score = 0
    try:
        for child in parent.iterdir():
            if not child.is_file():
                continue
            if child.suffix.casefold() not in AUDIO_SUFFIXES:
                continue
            score = _filename_match_score(child.name, wanted_name)
            if score > best_score:
                best_score = score
                best = child
    except OSError:
        return None
    if best is not None and best_score >= 82:
        try:
            return best.resolve()
        except OSError:
            return best
    return None


def _filename_match_score(actual: str, wanted: str) -> int:
    """Compare filenames including track-prefix / feat. variants."""

    if actual.casefold() == wanted.casefold():
        return 100

    actual_stem = Path(actual).stem
    wanted_stem = Path(wanted).stem
    if actual_stem.casefold() == wanted_stem.casefold():
        score = 98
    else:
        a = _normalize_track_key(actual_stem)
        w = _normalize_track_key(wanted_stem)
        if a == w:
            score = 96
        elif a and w and (a in w or w in a):
            score = 90
        else:
            score = _name_similarity(actual_stem, wanted_stem)

    if re.search(r"\(\d+\)$", actual_stem) and not re.search(r"\(\d+\)$", wanted_stem):
        score -= 20
    actual_ext = Path(actual).suffix.casefold()
    wanted_ext = Path(wanted).suffix.casefold()
    if wanted_ext and actual_ext != wanted_ext:
        if actual_ext in {".opus", ".webm", ".ogg"}:
            score -= 12
        else:
            score -= 4
    return max(0, score)


def _normalize_track_key(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "")
    text = text.casefold()
    # Track/disc prefix: "01-01 - Title", "01 - Title", "1. Title"
    text = re.sub(r"^\d{1,2}([._-]\d{1,2})?\s*[-_.]\s*", "", text)
    text = text.replace("featuring", "feat")
    text = text.replace("feat.", "feat")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _name_similarity(left: str, right: str) -> int:
    a = _normalize_track_key(left)
    b = _normalize_track_key(right)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 92
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0
    overlap = len(tokens_a & tokens_b)
    return int(100 * overlap / max(len(tokens_a), len(tokens_b)))
