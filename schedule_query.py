"""
"/saat Ç4 salı" gibi sorulara cevap üretir.

Telegram'dan habersizdir; metin alır, metin döner. Bilinen en güncel tarife
(hafta içi / hafta sonu) bot'un kaydettiği okunmuş PDF'lerden alınır, yani
sorgu için belediye sayfasına gitmez.

Kullanım örnekleri:
    /saat Ç4                 bugünün tüm kalkış saatleri
    /saat Ç4 salı            salı günü
    /saat Ç4 hafta sonu      hafta sonu tarifesi
    /saat Ç3 ekspres cuma    boşluklu hat adları da olur
    /saat Ç4 şimdi           şu andan sonraki 3 sefer (her kalkış durağı için)
    /saat Ç4 yarın şimdi     (kısaca "sonraki", "yarın" da çalışır)

NOT: PDF'ler yalnızca "hafta içi" ve "hafta sonu" diye ayrılıyor.
Pazartesi-cuma = hafta içi, cumartesi-pazar = hafta sonu tarifesi sayılır.
"""

from datetime import datetime, timedelta, timezone

import monitor
from pdf_parser import ascii_key

try:
    from zoneinfo import ZoneInfo
    TURKEY = ZoneInfo("Europe/Istanbul")
except Exception:                                    # tzdata yoksa: Türkiye sabit UTC+3
    TURKEY = timezone(timedelta(hours=3))


DAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

DAY_WORDS = {
    "PAZARTESI": 0, "PZT": 0,
    "SALI": 1, "SAL": 1,
    "CARSAMBA": 2, "CAR": 2,
    "PERSEMBE": 3, "PER": 3,
    "CUMA": 4, "CUM": 4,
    "CUMARTESI": 5, "CMT": 5,
    "PAZAR": 6, "PAZ": 6,
}

KIND_NAMES = {"weekday": "hafta içi", "weekend": "hafta sonu"}

NEXT_COUNT = 3
TIMES_PER_ROW = 8


# ======================================================================
# Tarifeleri yükle
# ======================================================================

def current_schedules():
    """{'weekday': tarife, 'weekend': tarife}: türüne göre bilinen en güncel tarife."""
    state = monitor.load_state()
    result = {}
    for kind in ("weekday", "weekend"):
        entries = [e for e in state["pdfs"].values() if e.get("kind") == kind]
        if not entries:
            continue
        latest = max(entries, key=monitor._effective_sort_key)
        snapshot = monitor.load_snapshot(latest["sha256"])
        if snapshot:
            result[kind] = snapshot
    return result


# ======================================================================
# Komut metnini çöz
# ======================================================================

def parse_request(text, now):
    """
    "Ç3 ekspres salı şimdi" -> {"line": "Ç3 ekspres", "weekday": 1|None,
                                "kind": None|"weekday"|"weekend", "next": bool}
    Gün/mod kelimeleri çıkarılır, geriye kalan hat adıdır.
    """
    tokens = text.split()
    day = None            # 0..6
    kind = None           # "weekday" | "weekend" (gün yerine tür seçildiyse)
    want_next = False
    rest = []

    i = 0
    while i < len(tokens):
        key = ascii_key(tokens[i])
        following = ascii_key(tokens[i + 1]) if i + 1 < len(tokens) else ""

        if key == "HAFTA" and following in ("ICI", "SONU"):
            kind = "weekday" if following == "ICI" else "weekend"
            i += 2
            continue
        if key in ("HAFTAICI", "HAFTASONU"):
            kind = "weekday" if key == "HAFTAICI" else "weekend"
        elif key in DAY_WORDS:
            day = DAY_WORDS[key]
        elif key == "BUGUN":
            day = now.weekday()
        elif key == "YARIN":
            day = (now.weekday() + 1) % 7
        elif key in ("SIMDI", "SONRAKI"):
            want_next = True
        else:
            rest.append(tokens[i])
        i += 1

    return {"line": " ".join(rest), "weekday": day, "kind": kind, "next": want_next}


def find_line(line_query, schedules):
    """Hat adını (Ç4, c4, ç-3 ekspres) PDF'teki adına eşler. (hat, öneriler) döner."""
    wanted = ascii_key(line_query)

    all_codes = sorted({code for s in schedules.values() for code in s["lines"] if code != "ÖĞRENCİ"})

    for code in all_codes:
        if ascii_key(code) == wanted:
            return code, []

    suggestions = [c for c in all_codes if wanted and wanted in ascii_key(c)]
    return None, suggestions or all_codes


# ======================================================================
# Biçimlendirme
# ======================================================================

def _rows(times):
    lines = []
    for start in range(0, len(times), TIMES_PER_ROW):
        lines.append("  ".join(times[start:start + TIMES_PER_ROW]))
    return "\n".join(lines)


def _schedule_label(schedule, kind):
    label = KIND_NAMES[kind] + " tarifesi"
    if schedule.get("effective"):
        label += f", uygulama: {schedule['effective']}"
    return label


def resolve_day(request, now):
    """İstekten (tür / gün / bugün) → (tarife türü, gösterilecek gün adı)."""
    if request["kind"]:
        kind = request["kind"]
        return kind, "Hafta içi" if kind == "weekday" else "Hafta sonu"

    weekday = request["weekday"] if request["weekday"] is not None else now.weekday()
    return ("weekend" if weekday >= 5 else "weekday"), DAY_NAMES[weekday]


def format_full_day(code, day_label, kind, schedule):
    line = schedule["lines"][code]
    out = [f"🚌 {code} — {day_label} ({_schedule_label(schedule, kind)})", ""]

    for stop, times in line["departures"].items():
        out.append(f"📍 {stop} kalkış — {len(times)} sefer")
        out.append(_rows(times))
        out.append("")

    if line.get("notes"):
        out.append("Özel / ilave seferler:")
        out.extend(f"• {note}" for note in line["notes"])
        out.append("")

    out.append("Not: Sadece kalkış saatleridir, ara durak geçiş saatleri yoktur.")
    return "\n".join(out)


def _upcoming(code, now, schedules, count):
    """
    Şu andan itibaren, her kalkış durağı için sonraki `count` sefer.
    Bugün yetmezse sonraki günlerin tarifesine geçer (en fazla 2 gün sonrasına).
    Döner: {durak: [(gün_ofseti, "HH:MM", tarife_türü), ...]}
    """
    result = {}
    now_hm = now.strftime("%H:%M")

    for offset in range(0, 3):
        day = (now + timedelta(days=offset)).weekday()
        kind = "weekend" if day >= 5 else "weekday"
        schedule = schedules.get(kind)
        if not schedule or code not in schedule["lines"]:
            continue

        for stop, times in schedule["lines"][code]["departures"].items():
            found = result.setdefault(stop, [])
            for t in times:
                if len(found) >= count:
                    break
                if offset == 0 and t < now_hm:
                    continue
                found.append((offset, t, kind))
    return result


def format_next(code, now, schedules):
    upcoming = _upcoming(code, now, schedules, NEXT_COUNT)

    out = [f"🚌 {code} — {DAY_NAMES[now.weekday()]} {now.strftime('%H:%M')} itibarıyla sonraki seferler", ""]
    if not any(upcoming.values()):
        out.append("Yaklaşan sefer bulunamadı.")
        return "\n".join(out)

    for stop, items in upcoming.items():
        parts = []
        for offset, t, _ in items:
            if offset == 0:
                parts.append(t)
            elif offset == 1:
                parts.append(f"{t} (yarın)")
            else:
                parts.append(f"{t} ({DAY_NAMES[(now + timedelta(days=offset)).weekday()]})")
        out.append(f"📍 {stop} kalkış: " + (", ".join(parts) if parts else "—"))

    out.append("")
    out.append("Not: Sadece kalkış saatleridir, ara durak geçiş saatleri yoktur.")
    return "\n".join(out)


def _line_codes(schedule):
    return [code for code in schedule["lines"] if code != "ÖĞRENCİ"]


def format_all_lines(day_label, kind, schedule):
    """Bir günün tüm hatları: [başlık, hat1 bloğu, hat2 bloğu, ...] (her hat ayrı blok)."""
    codes = _line_codes(schedule)
    blocks = [
        f"🚌 Tüm hatlar — {day_label} ({_schedule_label(schedule, kind)})\n"
        f"{len(codes)} hat: " + ", ".join(codes)
    ]

    for code in codes:
        line = schedule["lines"][code]
        out = [f"━━ {code} ━━"]
        for stop, times in line["departures"].items():
            out.append(f"📍 {stop} kalkış — {len(times)} sefer")
            out.append(_rows(times))
        if line.get("notes"):
            out.append("Özel / ilave seferler:")
            out.extend(f"• {note}" for note in line["notes"])
        blocks.append("\n".join(out))

    blocks.append("Not: Sadece kalkış saatleridir, ara durak geçiş saatleri yoktur.")
    return blocks


def format_next_all(now, schedules):
    """Şu andan sonraki 3 sefer: bütün hatlar, her kalkış durağı için."""
    codes = []
    for schedule in schedules.values():
        for code in _line_codes(schedule):
            if code not in codes:
                codes.append(code)

    blocks = [f"🚌 Tüm hatlar — {DAY_NAMES[now.weekday()]} {now.strftime('%H:%M')} itibarıyla sonraki seferler"]
    for code in codes:
        upcoming = _upcoming(code, now, schedules, NEXT_COUNT)
        if not any(upcoming.values()):
            continue
        out = [f"━━ {code} ━━"]
        for stop, items in upcoming.items():
            parts = []
            for offset, t, _ in items:
                parts.append(t if offset == 0 else f"{t} ({'yarın' if offset == 1 else DAY_NAMES[(now + timedelta(days=offset)).weekday()]})")
            out.append(f"📍 {stop}: " + (", ".join(parts) if parts else "—"))
        blocks.append("\n".join(out))

    blocks.append("Not: Sadece kalkış saatleridir, ara durak geçiş saatleri yoktur.")
    return blocks


def list_lines(schedules):
    lines = []
    for kind in ("weekday", "weekend"):
        schedule = schedules.get(kind)
        if schedule:
            codes = [c for c in schedule["lines"] if c != "ÖĞRENCİ"]
            lines.append(f"{KIND_NAMES[kind].capitalize()}: " + ", ".join(codes))
    return "\n".join(lines)


HELP = (
    "Kullanım:\n"
    "/saat Ç4 — bugünün saatleri\n"
    "/saat Ç4 salı — belirli bir gün\n"
    "/saat Ç4 hafta sonu — hafta sonu tarifesi\n"
    "/saat Ç3 ekspres cuma — boşluklu hat adı\n"
    "/saat Ç4 şimdi — sonraki 3 sefer\n"
    "/hepsi — bütün hatların saatleri (/hepsi cumartesi, /hepsi şimdi)\n"
    "/hatlar — hat listesi"
)


# ======================================================================
# Ana fonksiyon
# ======================================================================

def answer(text, now=None):
    """'/saat' komutunun argüman metnine cevap metni döner."""
    now = now or datetime.now(TURKEY)
    schedules = current_schedules()

    if not schedules:
        return "Henüz okunmuş bir tarife yok. Önce /kontrol yaz, birkaç saniye sonra tekrar dene."

    request = parse_request(text, now)

    if not request["line"]:
        return HELP + "\n\nHatlar:\n" + list_lines(schedules)

    code, suggestions = find_line(request["line"], schedules)
    if code is None:
        return (
            f"“{request['line']}” adında bir hat bulamadım.\n\n"
            "Mevcut hatlar:\n" + list_lines(schedules)
        )

    # ---- sonraki seferler ----
    if request["next"]:
        if request["weekday"] is not None or request["kind"]:
            return "“şimdi” sadece bugün için çalışır; gün seçmeden dene: /saat " + code + " şimdi"
        return format_next(code, now, schedules)

    # ---- tam gün listesi ----
    kind, day_label = resolve_day(request, now)

    schedule = schedules.get(kind)
    if not schedule:
        return f"Bilinen bir {KIND_NAMES[kind]} tarifesi yok."
    if code not in schedule["lines"]:
        other = "weekend" if kind == "weekday" else "weekday"
        hint = ""
        if other in schedules and code in schedules[other]["lines"]:
            hint = f"\n{KIND_NAMES[other].capitalize()} tarifesinde var: /saat {code} {KIND_NAMES[other]}"
        return f"{code} hattı {KIND_NAMES[kind]} tarifesinde yok.{hint}"

    return format_full_day(code, day_label, kind, schedule)


def answer_all(text, now=None):
    """
    '/hepsi' komutunun argüman metnine cevap: bloklardan oluşan liste
    (bot.py blokları kesmeden mesajlara paketler).

        /hepsi              bugün, bütün hatlar
        /hepsi cumartesi    belirli gün
        /hepsi hafta sonu   hafta sonu tarifesi
        /hepsi şimdi        şu andan sonraki 3 sefer, bütün hatlar
    """
    now = now or datetime.now(TURKEY)
    schedules = current_schedules()

    if not schedules:
        return ["Henüz okunmuş bir tarife yok. Biraz sonra tekrar dene."]

    request = parse_request(text, now)

    if request["next"]:
        if request["weekday"] is not None or request["kind"]:
            return ["“şimdi” sadece bugün için çalışır; gün seçmeden dene: /hepsi şimdi"]
        return format_next_all(now, schedules)

    kind, day_label = resolve_day(request, now)
    schedule = schedules.get(kind)
    if not schedule:
        return [f"Bilinen bir {KIND_NAMES[kind]} tarifesi yok."]

    return format_all_lines(day_label, kind, schedule)
