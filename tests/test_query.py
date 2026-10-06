"""/saat komutu: gerçek PDF'lerle, sabit bir 'şimdi' ile."""

from datetime import datetime
from pathlib import Path

import pytest

import schedule_query as q
from pdf_parser import parse_pdf

DATA = Path(__file__).resolve().parent.parent / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "7-EKIM-3.pdf").exists(), reason="örnek PDF'ler yok"
)

# 6 Ekim 2026 = Salı
TUESDAY = datetime(2026, 10, 6, 14, 32, tzinfo=q.TURKEY)


@pytest.fixture
def schedules(monkeypatch):
    data = {"weekday": parse_pdf(DATA / "7-EKIM-3.pdf"), "weekend": parse_pdf(DATA / "10-EKIM-18.pdf")}
    monkeypatch.setattr(q, "current_schedules", lambda: data)
    return data


def test_parse_request_words():
    r = q.parse_request("Ç3 ekspres salı", TUESDAY)
    assert r["line"] == "Ç3 ekspres" and r["weekday"] == 1 and not r["next"]
    assert q.parse_request("ç4 hafta sonu", TUESDAY)["kind"] == "weekend"
    assert q.parse_request("Ç4 şimdi", TUESDAY)["next"]
    assert q.parse_request("Ç4 yarın", TUESDAY)["weekday"] == 2
    assert q.parse_request("Ç4 Cumartesi", TUESDAY)["weekday"] == 5


def test_line_names_are_forgiving(schedules):
    assert q.find_line("ç-4", schedules)[0] == "Ç4"
    assert q.find_line("c3 ekspres", schedules)[0] == "Ç3 EKSPRES"
    assert q.find_line("Ç3", schedules)[0] == "Ç3"          # Ekspres ile karışmaz
    assert q.find_line("yok", schedules)[0] is None


def test_full_day_uses_right_timetable(schedules):
    text = q.answer("Ç4 salı", TUESDAY)
    assert "Salı" in text and "hafta içi" in text and "07:45" in text
    assert "BAHRİYE ÜÇOK CADDESİ" in text

    saturday = q.answer("Ç1 cumartesi", TUESDAY)
    assert "hafta sonu" in saturday and "07:05" in saturday


def test_default_is_today(schedules):
    assert "Salı" in q.answer("Ç1", TUESDAY)


def test_line_missing_on_that_day_gives_hint(schedules):
    text = q.answer("Ç4 cumartesi", TUESDAY)
    assert "yok" in text and "Ç4 EKSPRES" not in text


def test_next_departures(schedules):
    text = q.answer("Ç1 şimdi", TUESDAY)
    assert "14:32" in text and "sonraki" in text
    for line in text.splitlines():
        if line.startswith("📍"):
            times = [t.strip() for t in line.split(":", 1)[1].split(",")]
            assert len(times) == 3
            assert all(t >= "14:32" for t in times)


def test_next_rolls_into_tomorrow(schedules):
    late = datetime(2026, 10, 6, 23, 50, tzinfo=q.TURKEY)
    assert "yarın" in q.answer("Ç1 şimdi", late)


def test_unknown_line_lists_options(schedules):
    assert "Mevcut hatlar" in q.answer("Ç99", TUESDAY)
    assert "Kullanım" in q.answer("", TUESDAY)


def test_no_schedules_yet(monkeypatch):
    monkeypatch.setattr(q, "current_schedules", lambda: {})
    assert "/kontrol" in q.answer("Ç4", TUESDAY)


def test_all_lines_for_a_day(schedules):
    blocks = q.answer_all("", TUESDAY)
    text = "\n".join(blocks)
    assert "Salı" in blocks[0] and "hafta içi" in blocks[0]
    for code in ("Ç1", "Ç3 EKSPRES", "Ç4", "Ç11K EKSPRES", "ÇT2"):
        assert f"━━ {code} ━━" in text
    assert "ÖĞRENCİ" not in text and "━━ Ç4 EKSPRES" not in text    # Ç4 EKSPRES sadece hafta sonu

    weekend = "\n".join(q.answer_all("hafta sonu", TUESDAY))
    assert "Hafta sonu" in weekend and "━━ Ç4 EKSPRES ━━" in weekend


def test_all_lines_next_departures(schedules):
    blocks = q.answer_all("şimdi", TUESDAY)
    text = "\n".join(blocks)
    assert "14:32" in blocks[0] and "━━ Ç1 ━━" in text
    assert "“şimdi” sadece bugün" in q.answer_all("şimdi cuma", TUESDAY)[0]


def test_pack_blocks_keeps_blocks_whole_and_under_limit(schedules):
    import bot

    blocks = q.answer_all("", TUESDAY)
    messages = bot.pack_blocks(blocks, limit=3000)
    assert len(messages) > 1 and all(len(m) <= 3000 for m in messages)
    for block in blocks:                                  # hiçbir hat bloğu ikiye kesilmedi
        if len(block) <= 3000:
            assert any(block in m for m in messages)
