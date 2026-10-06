"""Ağa çıkmadan: sahte belediye sayfası + gerçek PDF'lerle takip akışı."""

import asyncio
from pathlib import Path

import pytest

import bot
import config
import monitor
import scraper
import subscribers

DATA = Path(__file__).resolve().parent.parent / "data"
BASE = "https://ulasim.canakkale.bel.tr/wp-content/uploads/2018/02/"

pytestmark = pytest.mark.skipif(
    not (DATA / "6-EKIM-13.pdf").exists(), reason="örnek PDF'ler yok"
)

LINKS = {
    "old": ("HAFTA İÇİ SEFER SAATLERİ", "6-EKIM-13.pdf"),
    "new": ("7 EKİM İTİBARİYLE HAFTA İÇİ SEFER SAATLERİ", "7-EKIM-3.pdf"),
    "weekend": ("HAFTA SONU SEFER SAATLERİ", "10-EKIM-18.pdf"),
}


class FakeSite:
    def __init__(self):
        self.visible = ["old", "weekend"]
        self.overrides = {}          # dosya adı -> bayt

    def page(self):
        links = "".join(
            f'<span><a href="{BASE}{LINKS[k][1]}">{LINKS[k][0]}</a></span>' for k in self.visible
        )
        return f"<html><body>{links}</body></html>"

    def download(self, schedule, etag=None, last_modified=None):
        name = schedule["filename"]
        content = self.overrides.get(name) or (DATA / name).read_bytes()
        return content, None, None


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(config, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bot, "OUTBOX_FILE", tmp_path / "outbox.json")

    fake = FakeSite()
    monkeypatch.setattr(scraper, "get_page", fake.page)
    monkeypatch.setattr(scraper, "download_pdf", fake.download)
    return fake


def test_full_lifecycle(site):
    # 1) İlk çalıştırma: sadece "takip başladı", fark bildirimi yok
    updates = monitor.check_for_updates()
    assert [u.kind for u in updates] == ["baseline"]
    assert "takibi başladı" in updates[0].text

    # 2) Hiçbir şey değişmedi
    assert monitor.check_for_updates() == []

    # 3) Belediye yeni hafta içi PDF'i yükledi → gerçek değişiklikler bildirilir
    site.visible = ["old", "new", "weekend"]
    updates = monitor.check_for_updates()
    assert [u.kind for u in updates] == ["changed"]
    text = updates[0].text
    assert "Ç3 EKSPRES" in text and "Ç4" in text and "Ç11K EKSPRES" in text
    assert "11:05" in text and "11:00" in text
    assert "Ç1" not in text.replace("Ç11", "")          # değişmeyen hatlar listelenmez
    assert updates[0].pdf_path and updates[0].pdf_path.exists()

    # 4) Aynı sayfa tekrar kontrol edilince tekrar bildirim yok
    assert monitor.check_for_updates() == []

    # 5) Aynı linkte dosya yeniden dışa aktarıldı (baytlar farklı, saatler aynı) → sessiz
    original = (DATA / "7-EKIM-3.pdf").read_bytes()
    site.overrides["7-EKIM-3.pdf"] = original + b"\n% yeniden aktarildi\n"
    assert monitor.check_for_updates() == []


def test_unreadable_pdf_warns_once(site):
    monitor.check_for_updates()                          # baseline
    site.visible = ["old", "new", "weekend"]
    site.overrides["7-EKIM-3.pdf"] = b"%PDF-1.4\nbu gecerli bir pdf degil\n"

    first = monitor.check_for_updates()
    assert [u.kind for u in first] == ["warning"]
    assert monitor.check_for_updates() == []             # aynı bozuk dosya için tekrar uyarma


def test_page_without_pdf_links_warns_once(site):
    monitor.check_for_updates()
    site.visible = []
    assert [u.kind for u in monitor.check_for_updates()] == ["warning"]
    assert monitor.check_for_updates() == []


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages, self.documents = [], []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram kapalı")
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **kwargs):
        if self.fail:
            raise RuntimeError("telegram kapalı")
        self.documents.append((chat_id, kwargs.get("filename") or Path(document).name))


def test_outbox_retries_after_telegram_failure(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [42])
    monitor.check_for_updates()
    site.visible = ["old", "new", "weekend"]
    updates = monitor.check_for_updates()
    assert updates and updates[0].kind == "changed"

    down = FakeBot(fail=True)
    asyncio.run(bot.deliver(down, updates))
    assert len(bot._load_outbox()) == 1                  # kaybolmadı

    up = FakeBot()
    asyncio.run(bot.deliver(up, []))                     # bir sonraki kontrolde tekrar dener
    assert up.messages and bot._load_outbox() == []
    assert any(name.endswith(".pdf") for _, name in up.documents)


def test_split_message_respects_limit():
    text = "\n".join(f"satır {i} " + "x" * 50 for i in range(300))
    chunks = bot.split_message(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# ----------------------------------------------------------------------
# Abonelikler
# ----------------------------------------------------------------------

class Forbidden(Exception):
    """telegram.error.Forbidden taklidi (bot engellenmiş)."""


class RecordingBot:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.sent = {}            # chat_id -> [metinler]
        self.documents = {}       # chat_id -> [dosya adları / id'ler]

    async def send_message(self, chat_id, text):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        self.sent.setdefault(chat_id, []).append(text)

    async def send_document(self, chat_id, document, **kwargs):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        self.documents.setdefault(chat_id, []).append(str(document))

        class _Doc:
            file_id = "FILE123"

        class _Msg:
            document = _Doc()
        return _Msg()


def _changed_update(site):
    monitor.check_for_updates()                      # baseline
    site.visible = ["old", "new", "weekend"]
    updates = monitor.check_for_updates()
    assert updates and updates[0].kind == "changed"
    return updates


def test_each_subscriber_gets_only_their_lines(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [1])
    subscribers.subscribe(2, "Sadece Ç4", ["Ç4"])
    subscribers.subscribe(3, "Sadece Ç1", ["Ç1"])        # Ç1 değişmedi
    subscribers.subscribe(4, "Hepsi", [])

    updates = _changed_update(site)
    fake = RecordingBot()
    asyncio.run(bot.deliver(fake, updates))

    admin_text = "\n".join(fake.sent[1])
    assert "Ç3 EKSPRES" in admin_text and "Ç4" in admin_text and "Ç11K EKSPRES" in admin_text

    only_c4 = "\n".join(fake.sent[2])
    assert "Ç4" in only_c4 and "Ç3 EKSPRES" not in only_c4 and "Ç11K" not in only_c4

    assert 3 not in fake.sent                            # ilgisiz değişiklik: hiç mesaj yok
    assert "Ç11K EKSPRES" in "\n".join(fake.sent[4])

    # PDF herkese gitti (ilk yüklemeden sonra file_id ile)
    assert 2 in fake.documents and 4 in fake.documents and 3 not in fake.documents
    assert "FILE123" in fake.documents[4]


def test_admin_only_updates_do_not_reach_subscribers(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [1])
    subscribers.subscribe(2, "Abone", [])

    fake = RecordingBot()
    asyncio.run(bot.deliver(fake, monitor.check_for_updates()))   # "takip başladı" mesajı
    assert 1 in fake.sent and 2 not in fake.sent


def test_blocked_subscriber_is_removed_and_others_still_served(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [1])
    subscribers.subscribe(2, "Engelleyen", [])
    subscribers.subscribe(4, "Normal", [])

    updates = _changed_update(site)
    fake = RecordingBot(blocked={2})
    asyncio.run(bot.deliver(fake, updates))

    assert not subscribers.is_subscriber(2)
    assert subscribers.is_subscriber(4) and 4 in fake.sent
    assert bot._load_outbox() == []                      # engelleyen için tekrar deneme yok


def test_retry_goes_only_to_those_who_failed(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [1])
    subscribers.subscribe(4, "Abone", [])

    updates = _changed_update(site)

    class FlakyBot(RecordingBot):
        async def send_message(self, chat_id, text):
            if chat_id == 4 and not getattr(self, "recovered", False):
                raise RuntimeError("geçici ağ hatası")
            await super().send_message(chat_id, text)

    flaky = FlakyBot()
    asyncio.run(bot.deliver(flaky, updates))
    assert 1 in flaky.sent and 4 not in flaky.sent
    assert bot._load_outbox()[0]["pending"] == [4]

    flaky.recovered = True
    first_count = len(flaky.sent[1])
    asyncio.run(bot.deliver(flaky, []))
    assert 4 in flaky.sent and len(flaky.sent[1]) == first_count   # yönetici ikinci kez almadı
    assert bot._load_outbox() == []


def test_subscribe_commands(site, monkeypatch):
    monkeypatch.setattr(config, "CHAT_IDS", [1])
    known = {"weekday": {"lines": {"Ç4": {}, "Ç3 EKSPRES": {}}}}

    # davet kodu gerekiyorsa
    monkeypatch.setattr(config, "SUBSCRIBE_CODE", "kod123")
    assert "davet kodu" in subscribers.subscribe_command(10, "X", "Ç4", schedules=known)
    assert not subscribers.is_subscriber(10)

    reply = subscribers.subscribe_command(10, "X", "KOD123 Ç4, ç3 ekspres, Ç2", schedules=known)
    assert "Abone oldun" in reply and "Ç2" in reply                # Ç2 şu an yok ama eklendi
    assert subscribers.get(10)["lines"] == ["Ç4", "Ç3 EKSPRES", "Ç2"]
    assert subscribers.line_keys(subscribers.get(10)) == {"C4", "C3EKSPRES", "C2"}

    # mevcut abone kod olmadan hatlarını değiştirebilir
    assert "güncellendi" in subscribers.subscribe_command(10, "X", "", schedules=known)
    assert subscribers.get(10)["lines"] == []

    # yönetici abone olamaz, abonelik iptali
    assert "yöneticisisin" in subscribers.subscribe_command(1, "Ben", "", schedules=known)
    assert "iptal edildi" in subscribers.unsubscribe_command(10)
    assert "Zaten abone değilsin" in subscribers.unsubscribe_command(10)


def test_subscriber_limit(site, monkeypatch):
    monkeypatch.setattr(config, "MAX_SUBSCRIBERS", 1)
    monkeypatch.setattr(config, "SUBSCRIBE_CODE", "")
    subscribers.subscribe_command(10, "A", "", schedules={})
    assert "sınır" in subscribers.subscribe_command(11, "B", "", schedules={})
