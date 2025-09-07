from __future__ import annotations

from parse.fill_columns import _normalize_label
from utils import group_id_from_name


def test_group_id_from_name_leading_digits():
    assert group_id_from_name("104б__Философия") == "104"


def test_group_id_from_name_slug_fallback():
    assert group_id_from_name("abc__DEF 123") == "abcdef123"


def test_normalize_label_spaces_and_underscores():
    assert _normalize_label(" 104б__Философия ") == "104б_философия"
    assert _normalize_label("104б   ___  Философия") == "104б_философия"
