"""
Bot ayarları.

Gizli bilgileri (bot token'ı gibi) koda yazma. Ya ortam değişkeni olarak ver
ya da proje klasöründeki `.env` dosyasına yaz (bkz. .env.example).
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path):
    """Çok basit .env okuyucu: ANAHTAR=değer satırları. Ortamdakini ezmez."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


# ==================================================
# BELEDİYE SAYFASI
# ==================================================

PAGE_URL = "https://ulasim.canakkale.bel.tr/rehber/hatlar-otobus-saatleri/"

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 30          # saniye
REQUEST_RETRIES = 3


# ==================================================
# KLASÖRLER
# ==================================================

DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"              # indirilen PDF'ler
SNAPSHOT_DIR = DATA_DIR / "snapshots"    # her PDF'in okunmuş hali (JSON)
STATE_FILE = DATA_DIR / "state.json"     # bot neyi gördü, neyi biliyor


# ==================================================
# TELEGRAM
# ==================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Bildirim gidecek sohbetler: "123456789" veya "123,-1001234567890"
CHAT_IDS = [
    int(part) for part in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",")
    if part
]


# Abonelik: botu bulan herkes /abone yazabilsin mi?
# Boşsa herkes abone olabilir. Doluysa "/abone KOD" gerekir (arkadaşlarına bu kodu ver).
SUBSCRIBE_CODE = os.environ.get("SUBSCRIBE_CODE", "").strip()
MAX_SUBSCRIBERS = int(os.environ.get("MAX_SUBSCRIBERS", "200"))


# ==================================================
# DAVRANIŞ
# ==================================================

CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))

# Sadece güzergah yazısı değiştiyse (saat aynıysa) bildirim gönderilsin mi?
NOTIFY_ON_ROUTE_ONLY = False

# İlk çalıştırmada "takip başladı" mesajı gönderilsin mi?
NOTIFY_ON_FIRST_RUN = True

# Bildirim mesajına yeni PDF de eklensin mi?
ATTACH_PDF = True

# Telegram bir mesajda en fazla 4096 karakter alır
TELEGRAM_MESSAGE_LIMIT = 4000
