from __future__ import annotations

from sync.telegram_bot.formatting import _parse_languages_from_grouped_title, build_language_bullets


def test_parse_grouped_language_title():
    title = "Английский г264, г267; Немецкий г236, г234"
    parts = _parse_languages_from_grouped_title(title)
    assert ("Английский", ["г264", "г267"]) in parts
    assert ("Немецкий", ["г236", "г234"]) in parts


def test_build_language_bullets():
    title = "Английский г264, г267; Немецкий г236"
    lines = build_language_bullets(title)
    assert any("Английский" in line and "г264" in line for line in lines)
    assert any("Немецкий" in line and "г236" in line for line in lines)
