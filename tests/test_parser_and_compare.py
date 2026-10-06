"""Gerçek belediye PDF'leriyle (data/ klasöründeki örnekler) uçtan uca denemeler."""

from pathlib import Path

import pytest

from comparator import compare_schedules, has_real_changes
from pdf_parser import parse_pdf

DATA = Path(__file__).resolve().parent.parent / "data"
OLD_WEEKDAY = DATA / "6-EKIM-13.pdf"
NEW_WEEKDAY = DATA / "7-EKIM-3.pdf"
WEEKEND = DATA / "10-EKIM-18.pdf"

pytestmark = pytest.mark.skipif(
    not (OLD_WEEKDAY.exists() and NEW_WEEKDAY.exists() and WEEKEND.exists()),
    reason="örnek PDF'ler data/ klasöründe yok",
)


@pytest.fixture(scope="module")
def old_weekday():
    return parse_pdf(OLD_WEEKDAY)


@pytest.fixture(scope="module")
def new_weekday():
    return parse_pdf(NEW_WEEKDAY)


def test_cover_info(new_weekday):
    assert new_weekday["kind"] == "weekday"
    assert new_weekday["effective"] == "6/10/2026"


def test_weekend_kind():
    assert parse_pdf(WEEKEND)["kind"] == "weekend"


def test_all_lines_found(new_weekday):
    expected = {"Ç1", "Ç3", "Ç3 EKSPRES", "Ç4", "Ç5", "Ç7", "Ç8", "Ç9", "Ç10", "Ç960",
                "ÇT3", "Ç11K", "Ç11K EKSPRES", "Ç11G", "Ç11Ç", "ÇT1", "ÇT2"}
    assert expected <= set(new_weekday["lines"])


def test_only_departures_are_extracted(new_weekday):
    """Ç4'te 10 sütun var ama sadece KALKIŞ olanlar alınmalı (VARIŞ ve geçiş değil)."""
    stops = set(new_weekday["lines"]["Ç4"]["departures"])
    assert stops == {"HÜSEYİN ELBİ YURT", "BAHRİYE ÜÇOK CADDESİ", "ESKİ HASTANE"}


def test_unlabelled_origin_column_is_a_departure(new_weekday):
    """ÇT-2'de çıkış durağının başlığında KALKIŞ yazmıyor."""
    stops = new_weekday["lines"]["ÇT2"]["departures"]
    assert stops["KEPEZ 144 TOKİ"] == ["08:00"]


def test_times_are_normalised(new_weekday):
    for line in new_weekday["lines"].values():
        for times in line["departures"].values():
            for t in times:
                assert len(t) == 5 and t[2] == ":"


def test_same_pdf_has_no_diff(new_weekday):
    diff = compare_schedules(new_weekday, new_weekday)
    assert not diff["changed"] and not diff["added_lines"] and not diff["removed_lines"]
    assert not has_real_changes(diff)


def test_real_changes_between_old_and_new_weekday(old_weekday, new_weekday):
    diff = compare_schedules(old_weekday, new_weekday)

    assert has_real_changes(diff)
    assert set(diff["changed"]) == {"Ç3 EKSPRES", "Ç4", "Ç11K EKSPRES"}

    # Metin farkıyla elle doğrulanmış: iki sefer 5 dk öne alınmış
    (stop,) = diff["changed"]["Ç11K EKSPRES"]["stops"]
    assert stop["removed"] == ["11:05", "13:05"]
    assert stop["added"] == ["11:00", "13:00"]


def test_filter_diff_keeps_only_requested_lines(old_weekday, new_weekday):
    from comparator import filter_diff

    diff = compare_schedules(old_weekday, new_weekday)
    only_c4 = filter_diff(diff, {"C4"})
    assert set(only_c4["changed"]) == {"Ç4"} and has_real_changes(only_c4)

    assert not has_real_changes(filter_diff(diff, {"C1"}))
    assert set(filter_diff(diff, {"C3EKSPRES", "C11KEKSPRES"})["changed"]) == {"Ç3 EKSPRES", "Ç11K EKSPRES"}
