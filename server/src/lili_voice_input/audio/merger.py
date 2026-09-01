from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher

MAX_OVERLAP_CHARACTERS = 120
MIN_FUZZY_OVERLAP_CHARACTERS = 4
FUZZY_OVERLAP_THRESHOLD = 0.82


def _normalize_with_positions(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    raw_positions: list[int] = []
    for index, character in enumerate(text):
        if character.isspace() or unicodedata.category(character).startswith("P"):
            continue
        for folded in character.casefold():
            normalized.append(folded)
            raw_positions.append(index)
    return "".join(normalized), raw_positions


def _raw_prefix_end(text: str, normalized_length: int) -> int:
    _, positions = _normalize_with_positions(text)
    if normalized_length <= 0:
        return 0
    if normalized_length > len(positions):
        return len(text)
    return positions[normalized_length - 1] + 1


def _is_cjk(character: str) -> bool:
    return "\u3400" <= character <= "\u9fff"


def _join_text(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    separator = "" if _is_cjk(left[-1]) or _is_cjk(right[0]) or unicodedata.category(left[-1]).startswith("P") else " "
    return f"{left}{separator}{right}"


def _boundary_overlap(left: str, right: str) -> int:
    maximum = min(len(left), len(right), MAX_OVERLAP_CHARACTERS)
    for size in range(maximum, 0, -1):
        if left[-size:] == right[:size]:
            return size
    for size in range(maximum, MIN_FUZZY_OVERLAP_CHARACTERS - 1, -1):
        if SequenceMatcher(None, left[-size:], right[:size], autojunk=False).ratio() >= FUZZY_OVERLAP_THRESHOLD:
            return size
    return 0


def merge_transcripts(existing: str, incoming: str) -> str:
    left = existing.strip()
    right = incoming.strip()
    if not left:
        return right
    if not right:
        return left
    normalized_left, _ = _normalize_with_positions(left)
    normalized_right, _ = _normalize_with_positions(right)
    overlap = _boundary_overlap(normalized_left, normalized_right)
    if not overlap:
        return _join_text(left, right)
    return _join_text(left, right[_raw_prefix_end(right, overlap) :])


def merge_transcript_segments(transcripts: list[str]) -> str:
    merged = ""
    for transcript in transcripts:
        merged = merge_transcripts(merged, transcript)
    return merged
