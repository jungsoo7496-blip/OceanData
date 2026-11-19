"""계약 uniqueItems 위반의 오류 모양 (통합 검토 SHARING-5, ADR-0088): 배열 자리 하나, 다시 나온 값은 메시지에."""

from __future__ import annotations

from app.errors import duplicate_items


def test_no_duplicates_is_no_error() -> None:
    assert duplicate_items("asset_ids", ["a", "b"], "자산 ID") == []
    assert duplicate_items("asset_ids", [], "자산 ID") == []


def test_one_error_at_the_array_naming_repeated_values_in_order() -> None:
    (err,) = duplicate_items("item_asset_ids", ["a", "b", "a", "c", "b", "a"], "자산 ID")
    assert (err["pointer"], err["code"]) == ("/item_asset_ids", "unique")
    assert err["message"] == "자산 ID가 중복됐습니다: a, b"


def test_message_lists_at_most_five_values() -> None:
    values = [f"v{i}" for i in range(8)] * 2
    (err,) = duplicate_items("asset_ids", values, "자산 ID")
    assert err["message"] == "자산 ID가 중복됐습니다: v0, v1, v2, v3, v4 외 3개"
