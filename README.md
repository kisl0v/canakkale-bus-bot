# Çanakkale otobüs tarifesi Telegram botu

Belediyenin [sefer saatleri sayfasını](https://ulasim.canakkale.bel.tr/rehber/hatlar-otobus-saatleri/)
izler. Yeni ya da güncellenmiş bir PDF çıkarsa:

1. PDF'i indirir
2. Hangi hatlar olduğunu okur (Ç1, Ç3, Ç3 EKSPRES, Ç4, ...)
3. Sadece **kalkış** saatlerini çıkarır (varış ve geçiş sütunlarını almaz)
4. Aynı türdeki (hafta içi / hafta sonu) önceki tarifeyle karşılaştırır
5. Kalkış saatlerinde **gerçek bir fark** varsa Telegram'dan özetini + yeni PDF'i gönderir

PDF'in baytları değişti ama saatler aynıysa (ör. belediye dosyayı yeniden dışa aktardıysa) sessiz kalır.

## Kurulum

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # TELEGRAM_BOT_TOKEN'ı doldur (@BotFather'dan)
python bot.py             # botu başlat, Telegram'da /start yaz → sohbet ID'ni öğren
# ID'yi .env'deki TELEGRAM_CHAT_ID'ye yaz, botu yeniden başlat
python bot.py --test      # test mesajı gelmeli
```

## Çalıştırma

| Komut | Ne yapar |
|---|---|
| `python bot.py` | Sürekli çalışır; her 30 dk'da bir kontrol eder; `/durum`, `/kontrol` komutlarına cevap verir |
| `python bot.py --once` | Tek kontrol yapıp çıkar (cron/launchd ile kullanmak için) |
| `python comparator.py ESKI.pdf YENI.pdf` | İki PDF'i Telegram'sız karşılaştırır |
| `python pdf_parser.py data/7-EKIM-3.pdf` | Bir PDF'ten çıkan hatları/kalkışları listeler |
| `python scraper.py` | Sayfadaki PDF linklerini listeler ve indirir |
| `pytest` | Testler (örnek PDF'ler `data/` içinde olmalı) |

İlk çalıştırmada bot sayfadaki mevcut tarifeleri "başlangıç" olarak kaydeder, fark bildirimi göndermez.

## Abonelik (arkadaşlar için)

`.env`'deki `TELEGRAM_CHAT_ID` sahibidir (yönetici): her bildirimi alır, `/kontrol` kullanabilir.
Diğer herkes kendi başına abone olur:

| Komut | Ne yapar |
|---|---|
| `/abone` | Bütün hatların değişikliklerine abone ol |
| `/abone Ç4, Ç11K` | Sadece bu hatlar (virgülle ayır; `Ç3` ile `Ç3 EKSPRES` ayrı hatlardır) |
| `/abone KOD Ç4` | `.env`'de `SUBSCRIBE_CODE` tanımlıysa davet kodu gerekir |
| `/iptal` | Abonelikten çık |
| `/saat Ç4 salı` | Bir hattın saatleri (gün, `hafta sonu`, `şimdi` seçilebilir) |
| `/hepsi` | Bütün hatların bugünkü saatleri (`/hepsi cumartesi`, `/hepsi hafta sonu`, `/hepsi şimdi`); `/tumu` da olur |
| `/hatlar`, `/durum` | Hat listesi, abonelik durumu |

`/saat`, `/hepsi`, `/hatlar` ve `/durum` sadece yönetici ve aboneler için çalışır.

Aboneler `data/subscribers.json`'da tutulur. Botu engelleyen abone otomatik silinir.
Teknik uyarılar ("PDF okunamadı" gibi) yalnızca yöneticiye gider.

## Dosyalar

- `scraper.py` — sayfadaki PDF linklerini bulur, indirir (koşullu GET ile)
- `pdf_parser.py` — PDF → `{hat: {kalkış durağı: [saatler]}}`
- `comparator.py` — iki tarifeyi karşılaştırır, Türkçe rapor üretir
- `monitor.py` — durum takibi (`data/state.json`), "yeni PDF mi?" kararı (SHA-256)
- `bot.py` — Telegram katmanı, zamanlayıcı, komutlar, giden kutusu
- `subscribers.py` — aboneler, `/abone` ve `/iptal` mantığı
- `schedule_query.py` — `/saat` komutunun mantığı
- `config.py` — ayarlar

## Bilmen gerekenler

- **Yeni PDF kararı dosya adına göre değil, içeriğe göre verilir.** Belediye hepsini
  `/2018/02/` klasörüne yüklüyor ve adlar tekrar kullanılabiliyor.
- **PDF okuma Excel'den gelen tabloya göre ayarlı.** Belediye tablo düzenini değiştirirse
  parser hat bulamayabilir; bot bu durumda bir kez "PDF okunamadı" uyarısı yollar.
- **Hata durumları:** sayfa 3 saat boyunca açılmazsa, sayfada PDF kalmazsa veya yeni PDF okunamazsa
  bir kez uyarı gelir. Telegram'a ulaşılamazsa bildirim `data/outbox.json`'da bekler ve tekrar denenir.
- Sayfa yalnızca güzergah yazısı değiştiyse bildirim gitmez (`config.NOTIFY_ON_ROUTE_ONLY = True`
  yaparak açabilirsin).
