"""
Telegram botu.

Çalıştırma:

    python bot.py            # sürekli çalışır: her N dakikada bir kontrol eder,
                             # /start /durum /kontrol komutlarına cevap verir
    python bot.py --once     # tek kontrol yapıp çıkar (cron / launchd için)
    python bot.py --test     # Telegram bağlantısını denemek için test mesajı yollar

Gerekli ayarlar (.env dosyası veya ortam değişkeni):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (bkz. .env.example)
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

import comparator
import config
import monitor
import schedule_query
import subscribers


log = logging.getLogger("bot")

OUTBOX_FILE = config.DATA_DIR / "outbox.json"

_check_lock = None           # aynı anda iki kontrol çalışmasın (event loop içinde oluşturulur)


# ======================================================================
# Mesaj hazırlama
# ======================================================================

def split_message(text, limit=None):
    """Uzun metni, satır sınırlarında bölerek Telegram limitine sığdırır."""
    limit = limit or config.TELEGRAM_MESSAGE_LIMIT
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:                       # tek satır bile limiti aşıyorsa
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


def pack_blocks(blocks, limit=None):
    """Blokları (her hat bir blok) kesmeden, limite sığacak kadar birleştirip mesajlara böler."""
    limit = limit or config.TELEGRAM_MESSAGE_LIMIT
    messages, current = [], ""
    for block in blocks:
        for piece in split_message(block, limit):          # tek başına limiti aşan blok bölünür
            if current and len(current) + 2 + len(piece) > limit:
                messages.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages


# ======================================================================
# Giden kutusu: Telegram'a ulaşılamazsa bildirim kaybolmasın
# ======================================================================

def _load_outbox():
    if not OUTBOX_FILE.exists():
        return []
    try:
        return json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_outbox(items):
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTBOX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OUTBOX_FILE)


def _serialize(update):
    return {
        "kind": update.kind,
        "audience": update.audience,
        "text": update.text,
        "details": update.details,
        "pdf_path": str(update.pdf_path) if update.pdf_path else None,
        "pdf_caption": update.pdf_caption,
        "meta": update.extra or None,
        "pending": None,                 # None = henüz kimseye gönderilmedi
    }


SEND_DELAY = 0.05                        # saniye; Telegram'ın saniyede ~30 mesaj sınırına takılmamak için


def _recipients(item):
    """{chat_id: hat_filtresi}  (filtre None = tüm hatlar). Yöneticiler her zaman her şeyi alır."""
    result = {chat_id: None for chat_id in config.CHAT_IDS}

    if item.get("audience") == "all":
        for chat_id in subscribers.all_ids():
            if chat_id not in result:
                result[chat_id] = subscribers.line_keys(subscribers.get(chat_id))
    return result


def _render(item, keys):
    """
    Alıcıya gidecek (metin, ayrıntı) çifti. Hat filtresi olan abone için fark
    sadece onun hatlarına indirgenir; hiç ilgili değişiklik yoksa (None, None).
    """
    meta = item.get("meta")
    if keys is None or not meta or "diff" not in meta:
        return item["text"], item["details"]

    diff = comparator.filter_diff(meta["diff"], keys)
    if not comparator.has_real_changes(diff, config.NOTIFY_ON_ROUTE_ONLY):
        return None, None

    args = (diff, meta.get("kind", "unknown"), meta.get("effective"), meta.get("title"))
    return comparator.summarize(*args), comparator.full_report(*args)


def _is_unreachable(error):
    """Kullanıcı botu engellemiş / sohbeti silmiş: bir daha denemenin anlamı yok."""
    name = type(error).__name__
    return name == "Forbidden" or (name == "BadRequest" and "chat not found" in str(error).lower())


async def _send_to(bot, chat_id, text, details, item, file_ids):
    chunks = split_message(text)
    for chunk in chunks:
        await bot.send_message(chat_id=chat_id, text=chunk)

    # Özet birden fazla mesaja bölündüyse tam raporu dosya olarak da ekle
    if details and len(chunks) > 1:
        await bot.send_document(
            chat_id=chat_id,
            document=details.encode("utf-8"),
            filename="tarife-farki.txt",
            caption="Ayrıntılı fark raporu",
        )

    pdf_path = item.get("pdf_path")
    if pdf_path and Path(pdf_path).exists():
        # PDF'i bir kez yükle, diğer alıcılara Telegram'ın file_id'siyle gönder
        document = file_ids.get(pdf_path) or Path(pdf_path)
        message = await bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=(item.get("pdf_caption") or "Yeni tarife PDF'i")[:1000],
        )
        file_id = getattr(getattr(message, "document", None), "file_id", None)
        if file_id:
            file_ids[pdf_path] = file_id


async def deliver(bot, updates):
    """
    Bildirimleri herkese yollar (yönetici + abone). Ulaşamadıklarımız giden
    kutusunda kalır; bir sonraki kontrolde sadece onlara tekrar denenir.
    """
    items = _load_outbox() + [_serialize(u) for u in updates]
    if not items:
        return

    if not config.CHAT_IDS and not subscribers.all_ids():
        log.warning("TELEGRAM_CHAT_ID tanımlı değil; bildirimler konsola yazılıyor.")
        for item in items:
            print("\n" + "=" * 60 + "\n" + item["text"])
        _save_outbox([])
        return

    remaining = []
    for item in items:
        recipients = _recipients(item)
        if item.get("pending") is not None:                   # önceki denemede ulaşamadıklarımız
            recipients = {cid: keys for cid, keys in recipients.items() if cid in item["pending"]}

        file_ids, failed = {}, []
        for chat_id, keys in recipients.items():
            text, details = _render(item, keys)
            if text is None:                                  # bu abone için ilgili değişiklik yok
                continue
            try:
                await _send_to(bot, chat_id, text, details, item, file_ids)
            except Exception as error:
                if _is_unreachable(error) and chat_id not in config.CHAT_IDS:
                    log.info("Abone botu engellemiş, listeden çıkarıldı: %s", chat_id)
                    subscribers.unsubscribe(chat_id)
                else:
                    log.exception("Telegram'a gönderilemedi (chat %s)", chat_id)
                    failed.append(chat_id)
            await asyncio.sleep(SEND_DELAY)

        if failed:
            item["pending"] = failed
            remaining.append(item)

    _save_outbox(remaining)
    if remaining:
        log.warning("%d bildirim bazı alıcılara gönderilemedi, sonra tekrar denenecek", len(remaining))


# ======================================================================
# Kontrol
# ======================================================================

async def run_check(bot):
    """Bir kez kontrol et, bildirimleri gönder. Gönderilen bildirim sayısını döner."""
    global _check_lock
    if _check_lock is None:
        _check_lock = asyncio.Lock()

    async with _check_lock:
        # requests ve pdfplumber bloklayıcı; event loop'u kilitlemesin
        updates = await asyncio.to_thread(monitor.check_for_updates)
        await deliver(bot, updates)
        return len(updates)


async def periodic_check(context):
    try:
        await run_check(context.bot)
    except Exception:
        log.exception("Periyodik kontrol hata verdi")


# ======================================================================
# Komutlar
# ======================================================================

def _is_admin(chat_id):
    return chat_id in config.CHAT_IDS


def _allowed(chat_id):
    """Sorgu komutlarını (/saat, /hatlar, /durum) kullanabilir: yönetici ya da abone."""
    return _is_admin(chat_id) or subscribers.is_subscriber(chat_id)


def _display_name(update):
    user = update.effective_user
    return (user.full_name if user else None) or str(update.effective_chat.id)


async def cmd_start(update, context):
    chat_id = update.effective_chat.id
    text = (
        "🚌 Çanakkale otobüs tarifesi botu\n\n"
        "Belediyenin sefer saati PDF'lerini takip ederim; kalkış saatleri "
        "değişince haber veririm.\n\n"
        "/abone — değişiklik bildirimlerine abone ol\n"
        "/abone Ç4, Ç11K — sadece bu hatlar için\n"
        "/iptal — abonelikten çık\n"
    )
    if _allowed(chat_id):
        text += (
            "\n/saat Ç4 salı — bir hattın kalkış saatleri (/saat yazarak kullanımı gör)\n"
            "/hepsi — bütün hatların saatleri (/hepsi cumartesi, /hepsi şimdi)\n"
            "/hatlar — hat listesi\n"
            "/durum — aboneliğin ve son kontrol\n"
        )
    if _is_admin(chat_id):
        text += "/kontrol — şimdi kontrol et\n"
    text += f"\nBu sohbetin ID'si: {chat_id}"
    await update.message.reply_text(text)


async def cmd_abone(update, context):
    chat_id = update.effective_chat.id
    reply = await asyncio.to_thread(
        subscribers.subscribe_command, chat_id, _display_name(update), " ".join(context.args)
    )
    await update.message.reply_text(reply)


async def cmd_iptal(update, context):
    await update.message.reply_text(subscribers.unsubscribe_command(update.effective_chat.id))


async def cmd_durum(update, context):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    text = subscribers.subscription_text(chat_id)
    if _is_admin(chat_id):
        text += "\n\n" + monitor.status_text()
    await update.message.reply_text(text)


async def cmd_saat(update, context):
    if not _allowed(update.effective_chat.id):
        await update.message.reply_text("Önce abone ol: /abone")
        return
    text = await asyncio.to_thread(schedule_query.answer, " ".join(context.args))
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def cmd_hepsi(update, context):
    if not _allowed(update.effective_chat.id):
        await update.message.reply_text("Önce abone ol: /abone")
        return
    blocks = await asyncio.to_thread(schedule_query.answer_all, " ".join(context.args))
    for message in pack_blocks(blocks):
        await update.message.reply_text(message)
        await asyncio.sleep(SEND_DELAY)


async def cmd_hatlar(update, context):
    if not _allowed(update.effective_chat.id):
        await update.message.reply_text("Önce abone ol: /abone")
        return
    schedules = await asyncio.to_thread(schedule_query.current_schedules)
    if not schedules:
        await update.message.reply_text("Henüz okunmuş bir tarife yok. Biraz sonra tekrar dene.")
        return
    await update.message.reply_text("Hatlar:\n" + schedule_query.list_lines(schedules))


async def cmd_kontrol(update, context):
    if not _is_admin(update.effective_chat.id):
        return
    await update.message.reply_text("Kontrol ediliyor…")
    count = await run_check(context.bot)
    if count == 0:
        await update.message.reply_text("Yeni bir değişiklik yok.")


# ======================================================================
# Başlatma
# ======================================================================

def _require_token():
    if not config.BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN tanımlı değil.\n"
            ".env.example dosyasını .env olarak kopyalayıp doldur."
        )


async def _once():
    from telegram import Bot

    _require_token()
    async with Bot(config.BOT_TOKEN) as bot:
        count = await run_check(bot)
    print(f"{count} bildirim işlendi." if count else "Yeni bir değişiklik yok.")


async def _test():
    from telegram import Bot

    _require_token()
    if not config.CHAT_IDS:
        raise SystemExit("TELEGRAM_CHAT_ID tanımlı değil. Botuna /start yazıp ID'yi öğrenebilirsin.")

    async with Bot(config.BOT_TOKEN) as bot:
        me = await bot.get_me()
        for chat_id in config.CHAT_IDS:
            await bot.send_message(chat_id=chat_id, text=f"✅ Test mesajı — @{me.username} çalışıyor.")
    print("Test mesajı gönderildi.")


def _run_forever():
    from telegram.ext import Application, CommandHandler

    _require_token()

    application = Application.builder().token(config.BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("durum", cmd_durum))
    application.add_handler(CommandHandler("kontrol", cmd_kontrol))
    application.add_handler(CommandHandler("saat", cmd_saat))
    application.add_handler(CommandHandler("hatlar", cmd_hatlar))
    application.add_handler(CommandHandler(["hepsi", "tumu"], cmd_hepsi))
    application.add_handler(CommandHandler("abone", cmd_abone))
    application.add_handler(CommandHandler("iptal", cmd_iptal))

    if application.job_queue is None:
        raise SystemExit(
            "Zamanlayıcı bulunamadı. Şunu çalıştır:\n"
            "    pip install \"python-telegram-bot[job-queue]\""
        )

    application.job_queue.run_repeating(
        periodic_check,
        interval=config.CHECK_INTERVAL_MINUTES * 60,
        first=10,                                   # açılışta 10 sn sonra ilk kontrol
        name="tarife-kontrol",
    )

    log.info("Bot başladı. Kontrol aralığı: %d dk", config.CHECK_INTERVAL_MINUTES)
    application.run_polling()


def main():
    parser = argparse.ArgumentParser(description="Çanakkale otobüs tarifesi Telegram botu")
    parser.add_argument("--once", action="store_true", help="tek kontrol yapıp çık")
    parser.add_argument("--test", action="store_true", help="test mesajı gönder")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # httpx her istekte INFO yazıyor; token'ı URL'de taşıdığı için kapatalım
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.test:
        asyncio.run(_test())
    elif args.once:
        asyncio.run(_once())
    else:
        _run_forever()


if __name__ == "__main__":
    main()
