"""
İki tarifeyi (pdf_parser.parse_pdf çıktısı) karşılaştırır.

Karşılaştırma sadece KALKIŞ saatleri üzerinden yapılır:

    hat  ->  durak (sütun)  ->  saatler

Bir hat için "gerçek değişiklik" şunlardır:
    * hat eklendi / kaldırıldı
    * bir duraktan kalkış saati eklendi / kaldırıldı
    * kalkış durağı eklendi / kaldırıldı
    * özel/ilave sefer notları değişti

Güzergah yazısındaki değişiklik ayrıca raporlanır ama TEK BAŞINA bildirim
nedeni sayılmaz (config.NOTIFY_ON_ROUTE_ONLY ile değiştirilebilir).
Sebep: Excel'den PDF'e aktarırken metin biraz oynayabilir.
"""

from collections import Counter

from pdf_parser import ascii_key


MAX_TIMES_SHOWN = 12


# ======================================================================
# Karşılaştırma
# ======================================================================

def _stop_index(departures):
    """{durak_adı: [saatler]} -> {anahtar: (görünen_ad, Counter(saatler))}"""
    index = {}
    for label, times in departures.items():
        key = ascii_key(label) or label
        index[key] = (label, Counter(times))
    return index


def _expand(counter):
    return sorted(counter.elements())


def compare_line(old, new):
    """Tek hattın iki sürümünü karşılaştırır. Fark yoksa None döner."""
    old_stops = _stop_index(old["departures"])
    new_stops = _stop_index(new["departures"])

    stop_changes = []
    for key in list(old_stops) + [k for k in new_stops if k not in old_stops]:
        if key in old_stops and key in new_stops:
            old_label, old_times = old_stops[key]
            new_label, new_times = new_stops[key]
            added = _expand(new_times - old_times)
            removed = _expand(old_times - new_times)
            if added or removed:
                stop_changes.append({
                    "stop": new_label,
                    "status": "changed",
                    "added": added,
                    "removed": removed,
                    "old_count": sum(old_times.values()),
                    "new_count": sum(new_times.values()),
                })
        elif key in old_stops:
            label, times = old_stops[key]
            stop_changes.append({
                "stop": label, "status": "removed",
                "added": [], "removed": _expand(times),
                "old_count": sum(times.values()), "new_count": 0,
            })
        else:
            label, times = new_stops[key]
            stop_changes.append({
                "stop": label, "status": "added",
                "added": _expand(times), "removed": [],
                "old_count": 0, "new_count": sum(times.values()),
            })

    # Notlar "07:35 - ÇATALTEPELİLER ..." gibi serbest metindir; Excel'den PDF'e
    # çıkarken satıra göre farklı bölünebilir. Kimlik olarak sadece saati kullan.
    old_notes = {n.split(" ", 1)[0]: n for n in old.get("notes", [])}
    new_notes = {n.split(" ", 1)[0]: n for n in new.get("notes", [])}
    notes_added = sorted(new_notes[k] for k in new_notes.keys() - old_notes.keys())
    notes_removed = sorted(old_notes[k] for k in old_notes.keys() - new_notes.keys())

    route_changed = (
        ascii_key(old.get("route", "")) != ascii_key(new.get("route", ""))
        and bool(old.get("route") or new.get("route"))
    )

    if not (stop_changes or notes_added or notes_removed or route_changed):
        return None

    return {
        "stops": stop_changes,
        "notes_added": notes_added,
        "notes_removed": notes_removed,
        "route_changed": route_changed,
        "old_route": old.get("route", ""),
        "new_route": new.get("route", ""),
    }


def compare_schedules(old, new):
    """
    İki tarifeyi karşılaştırır.

    Döner:
        {
          "added_lines":   [hat, ...],
          "removed_lines": [hat, ...],
          "changed":       {hat: compare_line(...)},
          "has_time_changes": bool,   # saat/durak/hat/not değişti mi?
          "route_only": bool,         # sadece güzergah yazısı mı değişti?
        }
    """
    old_lines, new_lines = old["lines"], new["lines"]

    added = [code for code in new_lines if code not in old_lines]
    removed = [code for code in old_lines if code not in new_lines]

    changed = {}
    for code in new_lines:
        if code in old_lines:
            diff = compare_line(old_lines[code], new_lines[code])
            if diff:
                changed[code] = diff

    return _finalize(added, removed, changed)


def _finalize(added, removed, changed):
    """Fark sözlüğünü ve türev bayrakları (has_time_changes, route_only) oluşturur."""
    has_time_changes = bool(added or removed) or any(
        d["stops"] or d["notes_added"] or d["notes_removed"] for d in changed.values()
    )
    route_only = bool(changed) and not has_time_changes

    return {
        "added_lines": added,
        "removed_lines": removed,
        "changed": changed,
        "has_time_changes": has_time_changes,
        "route_only": route_only,
    }


def filter_diff(diff, line_keys):
    """
    Farkı sadece belirli hatlara indirger (abonelerin hat filtresi için).
    line_keys: pdf_parser.ascii_key ile üretilmiş hat anahtarları, ör. {"C4", "C3EKSPRES"}.
    """
    def keep(code):
        return ascii_key(code) in line_keys

    return _finalize(
        [c for c in diff["added_lines"] if keep(c)],
        [c for c in diff["removed_lines"] if keep(c)],
        {c: d for c, d in diff["changed"].items() if keep(c)},
    )


def has_real_changes(diff, notify_on_route_only=False):
    """Bildirim göndermeye değer bir fark var mı?"""
    if diff["has_time_changes"]:
        return True
    return bool(diff["route_only"] and notify_on_route_only)


# ======================================================================
# Metin raporu
# ======================================================================

KIND_NAMES = {"weekday": "Hafta içi", "weekend": "Hafta sonu", "unknown": "Tarife"}


def _times(times, limit=MAX_TIMES_SHOWN):
    if len(times) <= limit:
        return ", ".join(times)
    return ", ".join(times[:limit]) + f" … (+{len(times) - limit})"


def summarize(diff, kind="unknown", effective=None, title=None):
    """Telegram mesajı için kısa özet (HTML değil, düz metin)."""
    lines = []

    header = f"🚌 {KIND_NAMES.get(kind, 'Tarife')} tarifesi değişti"
    if effective:
        header += f" (uygulama: {effective})"
    lines.append(header)
    if title:
        lines.append(title)
    lines.append("")

    n_changed = len(diff["changed"])
    lines.append(
        f"{n_changed} hatta değişiklik"
        + (f", {len(diff['added_lines'])} yeni hat" if diff["added_lines"] else "")
        + (f", {len(diff['removed_lines'])} kaldırılan hat" if diff["removed_lines"] else "")
    )
    lines.append("")

    if diff["added_lines"]:
        lines.append("➕ Yeni hatlar: " + ", ".join(diff["added_lines"]))
    if diff["removed_lines"]:
        lines.append("➖ Kaldırılan hatlar: " + ", ".join(diff["removed_lines"]))
    if diff["added_lines"] or diff["removed_lines"]:
        lines.append("")

    for code, change in diff["changed"].items():
        lines.append(f"▪ {code}")

        for stop in change["stops"]:
            if stop["status"] == "added":
                lines.append(f"   {stop['stop']}: yeni kalkış durağı ({stop['new_count']} sefer)")
                continue
            if stop["status"] == "removed":
                lines.append(f"   {stop['stop']}: kalkış durağı kaldırıldı ({stop['old_count']} sefer)")
                continue

            delta = stop["new_count"] - stop["old_count"]
            sign = f"{delta:+d}" if delta else "±0"
            lines.append(
                f"   {stop['stop']}: {stop['old_count']} → {stop['new_count']} sefer ({sign})"
            )
            if stop["removed"]:
                lines.append(f"      − {_times(stop['removed'])}")
            if stop["added"]:
                lines.append(f"      + {_times(stop['added'])}")

        for note in change["notes_added"]:
            lines.append(f"   ➕ not: {note}")
        for note in change["notes_removed"]:
            lines.append(f"   ➖ not: {note}")

        if change["route_changed"]:
            lines.append("   ℹ️ güzergah yazısı da değişmiş")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def full_report(diff, kind="unknown", effective=None, title=None):
    """Hiçbir şeyi kısaltmayan ayrıntılı rapor (dosya olarak gönderilir)."""
    lines = [summarize(diff, kind, effective, title).rstrip(), "", "=" * 60, "AYRINTILAR", "=" * 60, ""]

    for code, change in diff["changed"].items():
        lines.append(f"[{code}]")
        for stop in change["stops"]:
            lines.append(f"  {stop['stop']}  ({stop['old_count']} → {stop['new_count']})")
            if stop["removed"]:
                lines.append("    kaldırılan: " + ", ".join(stop["removed"]))
            if stop["added"]:
                lines.append("    eklenen:    " + ", ".join(stop["added"]))
        if change["route_changed"]:
            lines.append("  güzergah (eski): " + change["old_route"])
            lines.append("  güzergah (yeni): " + change["new_route"])
        for note in change["notes_removed"]:
            lines.append("  kaldırılan not: " + note)
        for note in change["notes_added"]:
            lines.append("  eklenen not:    " + note)
        lines.append("")

    return "\n".join(lines)


# ======================================================================
# Elle deneme:  python comparator.py data/6-EKIM-13.pdf data/7-EKIM-3.pdf
# ======================================================================

if __name__ == "__main__":
    import sys
    from pdf_parser import parse_pdf

    if len(sys.argv) != 3:
        print("Kullanım: python comparator.py ESKI.pdf YENI.pdf")
        raise SystemExit(1)

    old_schedule = parse_pdf(sys.argv[1])
    new_schedule = parse_pdf(sys.argv[2])
    result = compare_schedules(old_schedule, new_schedule)

    if not (result["added_lines"] or result["removed_lines"] or result["changed"]):
        print("Fark yok.")
    else:
        print(summarize(result, new_schedule["kind"], new_schedule["effective"]))
