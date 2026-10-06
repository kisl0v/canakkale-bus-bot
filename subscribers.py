"""
Aboneler: kim bildirim alıyor, hangi hatları takip ediyor.

    data/subscribers.json:
    {"123456789": {"name": "Ayşe", "lines": ["Ç4", "Ç11K"], "since": "2026-10-06T..."}}

lines boşsa kişi bütün hatların değişikliklerini alır.

Telegram'dan habersizdir; bot.py komut metnini buraya verir, cevap metni alır.
`.env`'deki TELEGRAM_CHAT_ID'dekiler "yöneticidir": abonelik gerekmeden her şeyi alırlar.
"""

import json
import os
from datetime import datetime, timezone

import config
import schedule_query
from pdf_parser import ascii_key


class LimitReached(Exception):
    pass


# ======================================================================
# Depolama
# ======================================================================

def _path():
    return config.DATA_DIR / "subscribers.json"


def load():
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get(chat_id):
    return load().get(str(chat_id))


def is_subscriber(chat_id):
    return str(chat_id) in load()


def all_ids():
    return [int(cid) for cid in load()]


def line_keys(entry):
    """Abonenin hat filtresi: {ascii anahtarlar} ya da None (= hepsi)."""
    if not entry or not entry.get("lines"):
        return None
    return {ascii_key(line) for line in entry["lines"]}


def subscribe(chat_id, name, lines):
    """Abone ekler ya da günceller. Yeni aboneyse True döner."""
    data = load()
    key = str(chat_id)
    is_new = key not in data

    if is_new and len(data) >= config.MAX_SUBSCRIBERS:
        raise LimitReached()

    data[key] = {
        "name": name,
        "lines": lines,
        "since": data[key]["since"] if not is_new else datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(data)
    return is_new


def unsubscribe(chat_id):
    data = load()
    if str(chat_id) not in data:
        return False
    del data[str(chat_id)]
    _save(data)
    return True


# ======================================================================
# Komutlar
# ======================================================================

def _describe(lines):
    return ", ".join(lines) if lines else "tüm hatlar"


def subscribe_command(chat_id, name, args_text, schedules=None):
    """
    /abone                  → bütün hatların değişikliklerini al
    /abone Ç4, Ç11K         → sadece bu hatlar (virgülle ayır; "Ç3 ekspres" boşluklu olabilir)
    /abone KOD Ç4           → davet kodu istenen kurulumlarda
    """
    if chat_id in config.CHAT_IDS:
        return "Sen bu botun yöneticisisin; zaten bütün bildirimleri alıyorsun."

    text = args_text.strip()
    already = is_subscriber(chat_id)

    # ---- davet kodu ----
    if config.SUBSCRIBE_CODE and not already:
        first, _, rest = text.partition(" ")
        if first.strip().lower() != config.SUBSCRIBE_CODE.lower():
            return "Abone olmak için davet kodu gerekli.\nÖrnek: /abone KOD  veya  /abone KOD Ç4, Ç11K"
        text = rest.strip()

    # ---- hatlar ----
    schedules = schedules if schedules is not None else schedule_query.current_schedules()

    lines, unknown = [], []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        code = schedule_query.find_line(part, schedules)[0] if schedules else None
        if code is None:
            code = part.upper().replace("  ", " ")
            if schedules:
                unknown.append(code)
        if ascii_key(code) not in {ascii_key(x) for x in lines}:
            lines.append(code)

    try:
        is_new = subscribe(chat_id, name, lines)
    except LimitReached:
        return "Üzgünüm, abone sınırına ulaşıldı."

    reply = ("✅ Abone oldun." if is_new else "✅ Aboneliğin güncellendi.")
    reply += f"\nBildirimler: {_describe(lines)}"
    if unknown:
        reply += (
            "\n\nŞu hatlar şu an tarifede yok ama listene ekledim (yeni çıkarsa haber alırsın): "
            + ", ".join(unknown)
        )
    reply += "\n\nTarife değişince haber vereceğim. Bırakmak için /iptal"
    return reply


def unsubscribe_command(chat_id):
    if chat_id in config.CHAT_IDS:
        return "Sen bu botun yöneticisisin; abonelikten çıkamazsın."
    if unsubscribe(chat_id):
        return "Abonelik iptal edildi. Tekrar abone olmak için /abone"
    return "Zaten abone değilsin. Abone olmak için /abone"


def subscription_text(chat_id):
    """/durum için tek satır."""
    if chat_id in config.CHAT_IDS:
        return f"Yönetici — her şeyi alıyorsun. Abone sayısı: {len(load())}"
    entry = get(chat_id)
    if not entry:
        return "Abone değilsin. Abone olmak için /abone"
    return f"Abonelik: {_describe(entry.get('lines'))}"
