"""
Takip mantığı: sayfayı kontrol et → yeni/değişmiş PDF'i bul → oku → eskisiyle
karşılaştır → bildirim gerekiyorsa `Update` listesi döndür.

Telegram'dan habersizdir; bot.py bu listeyi alıp gönderir. Böylece bütün
mantık Telegram'a bağlanmadan test edilebilir.

"Yeni PDF" kararı içeriğin SHA-256 özetine göre verilir:

    * Daha önce görmediğimiz bir link çıktıysa              → yeni PDF
    * Bildiğimiz link ama içerik (özet) değiştiyse           → güncellenmiş PDF
    * Aynı özet                                              → hiçbir şey yapma

Yeni PDF'in saatleri, aynı türdeki (hafta içi / hafta sonu) bir önceki
tarifeyle karşılaştırılır. Saat değişmediyse bildirim gitmez.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

import comparator
import config
import scraper
from pdf_parser import parse_pdf


log = logging.getLogger("monitor")

# Sayfa art arda bu kadar kez açılmazsa bir kere uyar (30 dk x 6 = 3 saat)
PAGE_FAILURES_BEFORE_ALERT = 6

# Yeni PDF'ten çıkan hat sayısı eskisinin bu oranının altındaysa okuma sorunu olabilir
SUSPICIOUS_LINE_RATIO = 0.6


@dataclass
class Update:
    kind: str                       # "changed" | "baseline" | "warning"
    text: str                       # Telegram mesajı
    details: str = ""               # ayrıntılı rapor (dosya olarak eklenir)
    pdf_path: Path = None           # eklenecek PDF
    pdf_caption: str = ""
    audience: str = "admin"         # "admin": sadece sahibi | "all": sahibi + aboneler
    extra: dict = field(default_factory=dict)   # "changed" için: diff, kind, effective, title


# ======================================================================
# Durum dosyası
# ======================================================================

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_state():
    return {"version": 1, "initialized": False, "seq": 0, "pdfs": {},
            "failures": {}, "page_failures": 0, "page_alerted": False,
            "no_pdf_alerted": False, "last_check": None, "last_success": None}


def load_state():
    if not config.STATE_FILE.exists():
        return _empty_state()
    try:
        state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.error("state.json bozuk, sıfırdan başlanıyor (yedek: state.json.bad)")
        config.STATE_FILE.replace(config.STATE_FILE.with_suffix(".json.bad"))
        return _empty_state()

    merged = _empty_state()
    merged.update(state)
    return merged


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_state(state):
    _write_atomic(config.STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def save_snapshot(sha, parsed):
    _write_atomic(config.SNAPSHOT_DIR / f"{sha}.json", json.dumps(parsed, ensure_ascii=False))


def load_snapshot(sha):
    path = config.SNAPSHOT_DIR / f"{sha}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ======================================================================
# Yardımcılar
# ======================================================================

def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _effective_sort_key(entry):
    """'6/10/2026' -> (2026, 10, 6). Tarih yoksa en eskiymiş gibi davran."""
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", entry.get("effective") or "")
    if not match:
        return (0, 0, 0, entry.get("seq", 0))
    day, month, year = map(int, match.groups())
    return (year, month, day, entry.get("seq", 0))


def _latest_previous(state, kind, exclude_url):
    """Aynı türdeki, bilinen en güncel tarife (kendisi hariç)."""
    candidates = [
        entry for url, entry in state["pdfs"].items()
        if url != exclude_url and entry.get("kind") == kind
    ]
    if not candidates:
        return None
    return max(candidates, key=_effective_sort_key)


def _title_for_message(schedule, parsed):
    title = schedule["title"] or schedule["filename"]
    return title


# ======================================================================
# Ana kontrol
# ======================================================================

def check_for_updates():
    """
    Sayfayı bir kez kontrol eder. Gönderilecek `Update` listesini döner.
    Hiçbir şey değişmediyse boş liste döner.
    """
    state = load_state()
    first_run = not state["initialized"]
    updates = []

    state["last_check"] = _now()

    # -------------------- sayfayı al --------------------
    try:
        html = scraper.get_page()
    except requests.RequestException as error:
        state["page_failures"] += 1
        log.warning("Sayfa alınamadı (%s. kez): %s", state["page_failures"], error)

        if state["page_failures"] >= PAGE_FAILURES_BEFORE_ALERT and not state["page_alerted"]:
            state["page_alerted"] = True
            updates.append(Update(
                "warning",
                f"⚠️ Belediye sayfasına {state['page_failures']} denemedir ulaşılamıyor.\n"
                f"Son hata: {error}\nBot denemeye devam ediyor.",
            ))
        save_state(state)
        return updates

    if state["page_alerted"]:
        updates.append(Update("warning", "✅ Belediye sayfasına yeniden ulaşılıyor."))
    state["page_failures"] = 0
    state["page_alerted"] = False

    schedules = scraper.find_schedules(html)

    if not schedules:
        log.warning("Sayfada hiç PDF linki bulunamadı")
        if not state["no_pdf_alerted"]:
            state["no_pdf_alerted"] = True
            updates.append(Update(
                "warning",
                "⚠️ Belediye sayfasında hiç PDF linki bulunamadı. "
                "Sayfa yapısı değişmiş olabilir; scraper.py'yi kontrol et.",
            ))
        save_state(state)
        return updates
    state["no_pdf_alerted"] = False

    # -------------------- her PDF'i işle --------------------
    for schedule in schedules:
        try:
            update = _process_schedule(state, schedule, first_run)
        except Exception:                                    # bir PDF diğerlerini engellemesin
            log.exception("PDF işlenirken beklenmeyen hata: %s", schedule["url"])
            continue

        if update:
            updates.append(update)
        save_state(state)

    # -------------------- ilk çalıştırma --------------------
    if first_run:
        state["initialized"] = True
        if config.NOTIFY_ON_FIRST_RUN and state["pdfs"]:
            lines = ["✅ Çanakkale otobüs tarifesi takibi başladı.", "", "İzlenen tarifeler:"]
            for entry in state["pdfs"].values():
                when = f" (uygulama: {entry['effective']})" if entry.get("effective") else ""
                lines.append(f"• {entry['title']}{when}")
            lines += ["", "Yeni bir tarife yayınlanıp saatler değişirse haber vereceğim."]
            updates.insert(0, Update("baseline", "\n".join(lines)))

    state["last_success"] = _now()
    save_state(state)
    return updates


def _process_schedule(state, schedule, first_run):
    """Tek bir PDF linkini işler. Bildirim gerekiyorsa Update döner."""
    url = schedule["url"]
    entry = state["pdfs"].get(url)

    # ---- indir ----
    try:
        content, etag, last_modified = scraper.download_pdf(
            schedule,
            etag=entry.get("etag") if entry else None,
            last_modified=entry.get("last_modified") if entry else None,
        )
    except scraper.NotModified:
        entry["last_seen"] = _now()
        return None
    except (requests.RequestException, ValueError) as error:
        log.warning("PDF indirilemedi (%s): %s", schedule["filename"], error)
        return None

    sha = _sha256(content)

    if entry and entry["sha256"] == sha:               # içerik aynı
        entry.update(last_seen=_now(), etag=etag, last_modified=last_modified)
        return None

    # ---- kaydet ve oku ----
    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(schedule["filename"]).stem
    pdf_path = config.PDF_DIR / f"{stem}-{sha[:8]}.pdf"
    pdf_path.write_bytes(content)

    try:
        parsed = parse_pdf(pdf_path)
        if not parsed["lines"]:
            raise ValueError("PDF'ten hiç hat okunamadı")
    except Exception as error:
        log.exception("PDF okunamadı: %s", schedule["filename"])
        already_alerted = state["failures"].get(url) == sha
        state["failures"][url] = sha                    # bir sonraki kontrolde tekrar denenir
        if already_alerted:
            return None
        return Update(
            "warning",
            f"⚠️ Yeni PDF yayınlanmış ama okunamadı: {schedule['title'] or schedule['filename']}\n"
            f"Hata: {error}\nPDF'in yapısı değişmiş olabilir; pdf_parser.py'yi kontrol et.",
            pdf_path=pdf_path if config.ATTACH_PDF else None,
        )

    state["failures"].pop(url, None)

    kind = parsed["kind"] if parsed["kind"] != "unknown" else schedule["type"]
    save_snapshot(sha, parsed)

    # ---- önceki sürümü bul ----
    previous_entry = None
    previous_sha = None
    if entry:                                           # aynı link, içerik değişmiş
        previous_sha = entry["sha256"]
    else:
        previous_entry = _latest_previous(state, kind, exclude_url=url)
        previous_sha = previous_entry["sha256"] if previous_entry else None

    state["seq"] += 1
    state["pdfs"][url] = {
        "sha256": sha,
        "filename": schedule["filename"],
        "pdf_file": pdf_path.name,
        "title": schedule["title"],
        "kind": kind,
        "effective": parsed["effective"],
        "lines": len(parsed["lines"]),
        "first_seen": entry["first_seen"] if entry else _now(),
        "last_seen": _now(),
        "etag": etag,
        "last_modified": last_modified,
        "seq": state["seq"],
    }

    if first_run:
        log.info("Başlangıç tarifesi kaydedildi: %s (%d hat)", schedule["filename"], len(parsed["lines"]))
        return None

    previous = load_snapshot(previous_sha) if previous_sha else None
    if previous is None:
        log.info("Karşılaştırılacak önceki tarife yok: %s", schedule["filename"])
        label = schedule["title"] or schedule["filename"]
        return Update(
            "baseline",
            f"🆕 Yeni tarife yayınlandı: {label}\n"
            f"Karşılaştırılacak önceki {comparator.KIND_NAMES.get(kind, 'tarife').lower()} "
            f"tarifesi bulunamadığı için fark çıkarılamadı.",
            pdf_path=pdf_path if config.ATTACH_PDF else None,
            pdf_caption=label,
            audience="all",
        )

    # ---- karşılaştır ----
    diff = comparator.compare_schedules(previous, parsed)

    if not comparator.has_real_changes(diff, config.NOTIFY_ON_ROUTE_ONLY):
        log.info("Yeni PDF (%s) ama kalkış saatlerinde değişiklik yok; bildirim yok.", schedule["filename"])
        return None

    title = _title_for_message(schedule, parsed)
    text = comparator.summarize(diff, kind, parsed["effective"], title)
    details = comparator.full_report(diff, kind, parsed["effective"], title)

    if len(parsed["lines"]) < SUSPICIOUS_LINE_RATIO * len(previous["lines"]):
        text = (
            f"⚠️ Dikkat: yeni PDF'ten {len(parsed['lines'])} hat okunabildi, "
            f"öncekinde {len(previous['lines'])} vardı. Okuma eksik olabilir.\n\n" + text
        )

    return Update(
        "changed",
        text,
        details=details,
        pdf_path=pdf_path if config.ATTACH_PDF else None,
        pdf_caption=title,
        audience="all",
        extra={"diff": diff, "kind": kind, "effective": parsed["effective"], "title": title},
    )


# ======================================================================
# /durum komutu için
# ======================================================================

def status_text():
    state = load_state()
    lines = ["🚌 Çanakkale otobüs tarifesi botu", ""]

    lines.append(f"Son kontrol: {state['last_check'] or 'henüz yok'}")
    lines.append(f"Son başarılı kontrol: {state['last_success'] or 'henüz yok'}")
    lines.append(f"Kontrol aralığı: {config.CHECK_INTERVAL_MINUTES} dk")
    lines.append("")

    if not state["pdfs"]:
        lines.append("Henüz izlenen tarife yok.")
    else:
        lines.append("İzlenen tarifeler:")
        for entry in sorted(state["pdfs"].values(), key=_effective_sort_key):
            when = f", uygulama: {entry['effective']}" if entry.get("effective") else ""
            lines.append(f"• {entry['title']} — {entry['lines']} hat{when}")

    return "\n".join(lines)
