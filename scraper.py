"""
Çanakkale Belediyesi ulaşım sayfasındaki sefer saati PDF'lerini bulur ve indirir.

Sayfa: https://ulasim.canakkale.bel.tr/rehber/hatlar-otobus-saatleri/

NOT: Belediye PDF'leri hep aynı klasöre (/wp-content/uploads/2018/02/) yüklüyor
ve dosya adları ("7-EKIM-3.pdf") yeniden kullanılabilir. Bu yüzden "yeni PDF mi?"
sorusunu dosya adına/linke bakarak değil, içeriğin SHA-256 özetine bakarak
cevaplıyoruz (bkz. monitor.py).
"""

import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

import config
from pdf_parser import ascii_key, tr_upper


# ==================================================
# SAYFAYI AL
# ==================================================

def get_page():
    """Belediye sayfasının HTML'ini döner. Geçici hatalarda birkaç kez dener."""
    last_error = None

    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                config.PAGE_URL,
                headers=config.REQUEST_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < config.REQUEST_RETRIES:
                time.sleep(2 * attempt)

    raise last_error


# ==================================================
# TARİHİ BUL
# ==================================================

MONTHS = {
    "OCAK": 1, "ŞUBAT": 2, "MART": 3, "NİSAN": 4, "MAYIS": 5, "HAZİRAN": 6,
    "TEMMUZ": 7, "AĞUSTOS": 8, "EYLÜL": 9, "EKİM": 10, "KASIM": 11, "ARALIK": 12,
}


def extract_date(text):
    """
    Yazının içinde "7 EKİM" gibi bir tarih arar.

        "7 EKİM İTİBARİYLE HAFTA İÇİ"  ->  (7, 10)

    Python'un upper()'ı "ekim"i "EKIM" yapar (noktalı İ olmaz); o yüzden
    Türkçe'ye uygun büyük harf kullanıyoruz.
    """
    import re

    text = tr_upper(text)

    for month_name, month_number in MONTHS.items():
        match = re.search(rf"(\d{{1,2}})\s*{month_name}", text)
        if match:
            return int(match.group(1)), month_number

    return None


# ==================================================
# TARİFE TİPİNİ BUL
# ==================================================

def detect_schedule_type(title):
    """weekday = hafta içi, weekend = hafta sonu"""
    key = ascii_key(title)

    if "HAFTASONU" in key:
        return "weekend"
    if "HAFTAICI" in key:
        return "weekday"
    return "unknown"


# ==================================================
# PDF LİNKLERİNİ VE BAŞLIKLARINI BUL
# ==================================================

def find_schedules(html):
    """Sayfadaki bütün PDF linklerini {title, type, day, month, url, filename} olarak döner."""
    soup = BeautifulSoup(html, "html.parser")

    schedules = []
    seen_urls = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if ".pdf" not in href.lower():
            continue

        pdf_url, _ = urldefrag(urljoin(config.PAGE_URL, href))

        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)

        # Başlık: linkin kendi yazısı, boşsa bulunduğu kutunun yazısı
        title = link.get_text(" ", strip=True)
        if not title and link.parent:
            title = link.parent.get_text(" ", strip=True)

        date = extract_date(title)

        schedules.append({
            "title": title,
            "type": detect_schedule_type(title),
            "day": date[0] if date else None,
            "month": date[1] if date else None,
            "url": pdf_url,
            "filename": Path(urlparse(pdf_url).path).name,
        })

    return schedules


# ==================================================
# PDF İNDİR
# ==================================================

class NotModified(Exception):
    """Sunucu 304 döndü: PDF son kontrolden beri değişmemiş."""


def download_pdf(schedule, etag=None, last_modified=None):
    """
    PDF'i indirir. Sunucu destekliyorsa koşullu istek atar (değişmediyse
    2 MB'lık dosyayı tekrar indirmez).

    Döner: (içerik_baytları, etag, last_modified)
    Fırlatır: NotModified, requests.RequestException, ValueError (PDF değilse)
    """
    headers = dict(config.REQUEST_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = requests.get(schedule["url"], headers=headers, timeout=config.REQUEST_TIMEOUT)

    if response.status_code == 304:
        raise NotModified()

    response.raise_for_status()

    content = response.content
    if not content.startswith(b"%PDF"):
        raise ValueError(f"PDF değil (ilk baytlar: {content[:20]!r}): {schedule['url']}")

    return content, response.headers.get("ETag"), response.headers.get("Last-Modified")


# ==================================================
# SONUÇLARI GÖSTER
# ==================================================

def print_schedules(schedules):
    print()
    print("=" * 60)
    print("📚 BULUNAN TARİFELER")
    print("=" * 60)

    for index, schedule in enumerate(schedules, start=1):
        print()
        print(f"Tarife #{index}")
        print("Başlık:", schedule["title"])
        print("Tip:", schedule["type"])

        if schedule["day"] is not None:
            print(f"Başlangıç tarihi: {schedule['day']:02d}.{schedule['month']:02d}")
        else:
            print("Başlangıç tarihi: belirtilmemiş")

        print("PDF:", schedule["filename"])
        print("URL:", schedule["url"])


# ==================================================
# ANA PROGRAM (elle deneme)
# ==================================================

def main():
    print("=" * 60)
    print("🚌 ÇANAKKALE ULAŞIM SCRAPER")
    print("=" * 60)

    print("🌐 Belediye sayfası indiriliyor...")
    schedules = find_schedules(get_page())
    print_schedules(schedules)

    config.PDF_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print("📥 PDF DOSYALARI")
    print("=" * 60)

    for schedule in schedules:
        try:
            content, _, _ = download_pdf(schedule)
        except (requests.RequestException, ValueError) as error:
            print("❌ İndirme hatası:", error)
            continue

        path = config.PDF_DIR / schedule["filename"]
        path.write_bytes(content)
        print("✅ Kaydedildi:", path)


if __name__ == "__main__":
    main()
