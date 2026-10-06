"""
Çanakkale Belediyesi sefer saati PDF'lerini okur.

PDF'ler Excel'den dışa aktarıldığı için sayfa yapısı şöyledir:

    * Her sayfa = bir hat (Ç-1, Ç-3, Ç-3 EKSPRES, ...)
    * Sayfanın üstünde güzergah yazısı, altında bir tablo var.
    * Tablonun sütunları duraklardır; başlıkta "KALKIŞ" / "VARIŞ" yazar.
      Arada başlığında hiçbir şey yazmayan "geçiş" sütunları vardır.

Biz sadece KALKIŞ saatlerini istiyoruz. Düz metin (pdftotext) satırlarına
güvenmek yerine her saatin x-koordinatına bakıp hangi sütuna ait olduğunu
buluyoruz. Böylece boş hücreler ve kaymış satırlar sorun çıkarmıyor.

Çıktı:
    {
      "kind": "weekday" | "weekend" | "unknown",
      "effective": "6/10/2026" | None,
      "lines": {
          "Ç4": {
              "page": 6,
              "route": "TERZİOĞLU KAMPÜS - ...",
              "departures": {"HÜSEYİN ELBİ YURT": ["07:50", ...], ...}
          }, ...
      }
    }
"""

import re
import statistics
from collections import defaultdict
from pathlib import Path

import pdfplumber


TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?!\d)")
PURE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
LINE_CODE_RE = re.compile(r"^(ÇT|Ç)\s*-?\s*(\d+[KGÇ]?)(?![A-ZÇĞİÖŞÜ0-9])")

CLUSTER_GAP = 17      # pt: bundan yakın saatler aynı sütundur (sütunlar >= 35 pt arayla)
WORD_GAP = 1.6        # pt: harfler arası boşluk bundan büyükse yeni kelime
OUTER_BAND = 32       # pt: ilk/son sütunun dışına taşabilen başlık genişliği


# ======================================================================
# Küçük yardımcılar
# ======================================================================

def tr_upper(text):
    """Türkçe büyük harf: 'ekim' -> 'EKİM' (Python'un upper()'ı i'yi I yapar)."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def ascii_key(text):
    """Karşılaştırma için: büyük harf, Türkçe harfler ASCII, sadece harf/rakam."""
    table = str.maketrans("İıŞşĞğÜüÖöÇç", "IiSsGgUuOoCc")
    text = text.translate(table).upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def normalize_time(text):
    match = TIME_RE.match(text)
    hour, minute = int(match.group(1)), int(match.group(2))
    return f"{hour:02d}:{minute:02d}"


def detect_kind(text):
    """Kapak yazısından hafta içi / hafta sonu bilgisi."""
    key = ascii_key(text)
    if "HAFTASONU" in key:
        return "weekend"
    if "HAFTAICI" in key:
        return "weekday"
    return "unknown"


# ======================================================================
# Sayfa içi: harfleri kelimelere, satırlara çevir
# ======================================================================

def chars_to_rows(chars, ytol=3):
    """Harfleri satırlara, satırları kelimelere ayırır."""
    rows = defaultdict(list)
    for ch in chars:
        rows[round(ch["top"] / ytol)].append(ch)

    result = []
    for key in sorted(rows):
        row = sorted(rows[key], key=lambda c: c["x0"])
        words = []
        current = []
        for ch in row:
            if ch["text"].strip() == "":
                if current:
                    words.append(current)
                    current = []
                continue
            if current and ch["x0"] - current[-1]["x1"] > WORD_GAP:
                words.append(current)
                current = []
            current.append(ch)
        if current:
            words.append(current)

        parsed = []
        for word in words:
            parsed.append({
                "text": "".join(c["text"] for c in word),
                "x0": word[0]["x0"],
                "x1": word[-1]["x1"],
                "font": word[0]["fontname"],
            })
        if parsed:
            result.append({"top": row[0]["top"], "words": parsed})
    return result


def row_text(row):
    return " ".join(w["text"] for w in row["words"])


def is_tab_name_row(row):
    """Excel sayfa adı: sayfanın en üstünde, küçücük Times New Roman yazısı."""
    return row["top"] < 30 and all("TimesNewRoman" in w["font"] for w in row["words"])


# ======================================================================
# Sayfa içi: saat satırları, tablo blokları, sütunlar
# ======================================================================

HEADER_ROW_GAP = 7    # pt: başlık satırları birbirine bu kadar yakın durur
KEYWORDS = ("KALKIS", "VARIS", "HAREKET")


NOTE_MIN_CHARS = 12   # saatin yanındaki açıklama bundan uzunsa "not"tur


def attach_times(rows):
    """
    Her satıra 'times' ve 'notes' ekler.

    * times: tablodaki sefer saatleri [{'time','x','top'}]
    * notes: saatin yanında uzun bir açıklama (veya "-" işareti) varsa ("07:30 - JANDARMA
      IŞIKLARINDAN - Ç960 ...", "İLAVE 08:10 KEPEZ MEYDAN - ...",
      "15:30 İTİBARİYLE ...") sütuna oturmayan özel seferdir; metin olarak
      saklanır.
    """
    pure_widths = [
        w["x1"] - w["x0"]
        for row in rows for w in row["words"]
        if PURE_TIME_RE.match(w["text"])
    ]
    typical_width = statistics.median(pure_widths) if pure_widths else 20

    for row in rows:
        words = row["words"]
        times, notes = [], []
        for index, word in enumerate(words):
            if not TIME_RE.match(word["text"]):
                continue

            pure = bool(PURE_TIME_RE.match(word["text"]))

            if not pure and re.fullmatch(r"\d{1,2}:\d{2}-", word["text"]):
                notes.append(f"{normalize_time(word['text'])} -")
                continue

            if pure:
                # Bu saatten sonraki saat olmayan kelimeler = açıklama
                tail = []
                for other in words[index + 1:]:
                    if TIME_RE.match(other["text"]):
                        break
                    tail.append(other["text"])
                comment = " ".join(tail).strip()
                if len(comment) >= NOTE_MIN_CHARS or comment.startswith("-"):
                    notes.append(f"{normalize_time(word['text'])} {comment}")
                    continue
                center = (word["x0"] + word["x1"]) / 2
            else:
                # "18:50-BAHRİYE" gibi yapışık hücre: sadece saat kısmının ortası
                center = word["x0"] + typical_width / 2

            times.append({"time": normalize_time(word["text"]), "x": center, "top": row["top"]})
        row["times"] = times
        row["notes"] = notes


def has_keyword(rows):
    key = ascii_key(" ".join(row_text(r) for r in rows))
    return any(k in key for k in KEYWORDS)


def header_chain(pending):
    """
    Saat satırlarından hemen önceki başlık satırları: alttan başlayıp,
    satırlar birbirine yakın durdukça yukarı çık. (Güzergah yazıları ve
    önceki bloğun notları daha uzakta kalır.)
    """
    pending = [r for r in pending if not is_tab_name_row(r)]
    if not pending:
        return []
    chain = [pending[-1]]
    for row in reversed(pending[:-1]):
        if chain[0]["top"] - row["top"] <= HEADER_ROW_GAP:
            chain.insert(0, row)
        else:
            break
    return chain


def split_blocks(rows):
    """
    Sayfayı tablo bloklarına böler. Bir sayfada, sütun yapısı farklı birden
    fazla tablo olabilir (ör. Ç-4'te akşam seferleri için ikinci başlık var).
    Yeni blok = içinde KALKIŞ/VARIŞ/HAREKET geçen yeni bir başlık.
    """
    blocks = []
    pending = []
    for row in rows:
        if row["times"] or row["notes"]:
            chain = header_chain(pending)
            if not blocks:
                blocks.append({"header": chain, "pre": pending[:len(pending) - len(chain)], "rows": []})
            elif chain and has_keyword(chain):
                blocks.append({"header": chain, "pre": [], "rows": []})
            blocks[-1]["rows"].append(row)
            pending = []
        else:
            pending.append(row)
    return blocks


def cluster_columns(time_items):
    """Saatleri x konumuna göre sütunlara ayırır."""
    ordered = sorted(time_items, key=lambda t: t["x"])
    clusters = []
    for item in ordered:
        if clusters and item["x"] - clusters[-1][-1]["x"] <= CLUSTER_GAP:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return clusters


STRAY_MAX = 2         # bu kadar saatlik küme "serseri" sayılabilir
STRAY_NEIGHBOUR_MIN = 10
STRAY_DISTANCE = 30   # pt


def absorb_strays(clusters):
    """
    Bir sütunun hemen yanına kayan 1-2 saatlik küçük kümeleri (hücre içi
    açıklama yüzünden kayanlar) büyük komşu sütuna katar. Böylece başlık
    bandı bölünüp etiketler bozulmaz.
    """
    centers = [statistics.mean(t["x"] for t in c) for c in clusters]
    keep = []
    for i, cluster in enumerate(clusters):
        if len(cluster) <= STRAY_MAX:
            neighbours = [
                j for j, other in enumerate(clusters)
                if j != i and len(other) >= STRAY_NEIGHBOUR_MIN
                and abs(centers[j] - centers[i]) <= STRAY_DISTANCE
            ]
            if neighbours:
                target = min(neighbours, key=lambda j: abs(centers[j] - centers[i]))
                clusters[target].extend(cluster)
                continue
        keep.append(cluster)
    return [c for c in keep if c]


def line_code_from_row(row):
    match = LINE_CODE_RE.match(tr_upper(row_text(row)).strip())
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}"


def column_label(header_rows, low, high):
    """[low, high) x aralığına denk gelen başlık kelimelerini birleştirir."""
    parts = []
    for row in header_rows:
        for word in row["words"]:
            center = (word["x0"] + word["x1"]) / 2
            if low <= center < high:
                parts.append(word["text"])
    return " ".join(parts)


def rescue_label(header_rows, low, high):
    """
    Bazı başlıklar birleştirilmiş hücredir ("ÇANAKKALE HAREKET" gibi) ve
    metin sütunun üstüne ortalanmaz. Anahtar kelimenin solundaki bitişik
    kelimeleri de al.
    """
    parts = []
    for row in header_rows:
        words = row["words"]
        for index, word in enumerate(words):
            center = (word["x0"] + word["x1"]) / 2
            if not (low <= center < high) or ascii_key(word["text"]) not in KEYWORDS:
                continue
            j = index - 1
            while j >= 0 and words[j + 1]["x0"] - words[j]["x1"] <= 6:
                if ascii_key(words[j]["text"]) not in KEYWORDS:
                    parts.insert(0, words[j]["text"])
                j -= 1
    return " ".join(parts)


def classify_label(label):
    key = ascii_key(label)
    if "KALKIS" in key:
        return "KALKIS"
    if "VARIS" in key:
        return "VARIS"
    if "HAREKET" in key:
        return "KALKIS"          # Çınarlı hattında "HAREKET" = kalkış
    return "GECIS"


def clean_label(label):
    """Başlıktaki KALKIŞ/VARIŞ/HAREKET kelimelerini at, geriye durak adı kalsın."""
    words = [w for w in label.split() if ascii_key(w) not in KEYWORDS]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def pick_departure_columns(columns):
    """
    Hangi sütunlar kalkış?

    * Başlığında KALKIŞ (veya HAREKET) yazanlar.
    * Her seyahat grubunun ilk sütunu (başlığı boş olsa bile). Bir grup
      bir VARIŞ sütununda biter; ya da solundaki sütunla hiçbir satırı
      paylaşmayan sütun yeni grubu başlatır. (ÇT-2'de çıkış durağının
      başlığında "KALKIŞ" yazmıyor.)
    * Blokta hiç KALKIŞ/VARIŞ yoksa (öğrenci seferleri) hepsi.
    """
    kinds = {c["kind"] for c in columns}
    if "KALKIS" not in kinds and "VARIS" not in kinds:
        return list(columns)

    chosen = []
    group_start = True
    previous = None
    for column in columns:
        if previous is not None and not (previous["rows"] & column["rows"]):
            group_start = True
        if column["kind"] != "VARIS" and (column["kind"] == "KALKIS" or group_start):
            chosen.append(column)
        group_start = column["kind"] == "VARIS"
        previous = column
    return chosen


def block_columns(block):
    """Bir bloğun sütunlarını (etiket, tür, saatler) çıkarır."""
    items = [t for row in block["rows"] for t in row["times"]]

    # Aynı görsel satırdaki hücreler 1-2 pt farklı y'de olabilir: satır kimliği ver.
    row_id, last_top = 0, None
    for item in sorted(items, key=lambda t: t["top"]):
        if last_top is not None and item["top"] - last_top > 4:
            row_id += 1
        item["row_id"] = row_id
        last_top = item["top"]

    clusters = absorb_strays(cluster_columns(items))
    centers = [statistics.mean(t["x"] for t in c) for c in clusters]

    columns = []
    for i, cluster in enumerate(clusters):
        low = centers[i] - OUTER_BAND if i == 0 else (centers[i - 1] + centers[i]) / 2
        high = (
            centers[i] + OUTER_BAND
            if i == len(clusters) - 1
            else (centers[i] + centers[i + 1]) / 2
        )

        label = column_label(block["header"], low, high)
        kind = classify_label(label)
        label = clean_label(label)
        if not label:
            label = clean_label(rescue_label(block["header"], low, high))

        columns.append({
            "index": i + 1,
            "x": centers[i],
            "kind": kind,
            "label": label,
            "times": sorted(t["time"] for t in cluster),
            "rows": {t["row_id"] for t in cluster},
        })
    return columns


# ======================================================================
# Sayfa → hat
# ======================================================================

def parse_page(page):
    rows = chars_to_rows(page.chars)
    attach_times(rows)
    blocks = split_blocks(rows)
    if not blocks:
        return None

    first = blocks[0]

    # -------- hat kodu --------
    line_code = None
    for row in first["pre"] + first["header"]:
        if is_tab_name_row(row):
            continue
        line_code = line_code_from_row(row)
        if line_code:
            break

    header_key = ascii_key(" ".join(row_text(r) for r in first["pre"] + first["header"]))
    if line_code and ("EKSPRES" in header_key or "EKPRES" in header_key):
        line_code += " EKSPRES"

    if line_code is None:
        all_key = ascii_key(" ".join(row_text(r) for r in rows))
        line_code = "ÖĞRENCİ" if "OGRENCI" in all_key else "BİLİNMEYEN"

    # -------- güzergah yazısı (bilgi amaçlı) --------
    route_rows = []
    for row in first["pre"]:
        text = row_text(row)
        if len(text) > 25 and "-" in text and not is_tab_name_row(row):
            route_rows.append(text)
    route = " | ".join(route_rows)

    # -------- kalkış sütunları (bütün bloklar birleşir) --------
    departures = {}
    for block in blocks:
        columns = block_columns(block)
        for column in pick_departure_columns(columns):
            label = column["label"] or f"SÜTUN {column['index']}"
            departures.setdefault(label, []).extend(column["times"])

    for label in departures:
        departures[label].sort()

    notes = sorted({note for row in rows for note in row["notes"]})

    return {
        "code": line_code,
        "route": route,
        "departures": departures,
        "notes": notes,
        "blocks": blocks,
    }


# ======================================================================
# Kapak sayfası
# ======================================================================

def parse_cover(page):
    text = page.extract_text() or ""
    kind = detect_kind(text)

    effective = None
    match = re.search(r"BAŞLAMA\s+TARİHİ\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", tr_upper(text))
    if match:
        effective = match.group(1)

    return kind, effective


# ======================================================================
# Ana fonksiyon
# ======================================================================

def parse_pdf(path):
    """PDF'i okuyup {kind, effective, lines} sözlüğü döner."""
    path = Path(path)

    lines = {}
    with pdfplumber.open(path) as pdf:
        kind, effective = parse_cover(pdf.pages[0])

        for page_number, page in enumerate(pdf.pages[1:], start=2):
            parsed = parse_page(page)
            if parsed is None:
                continue

            key = parsed["code"]
            if key in lines:                       # aynı kod iki sayfada
                key = f"{key} (sayfa {page_number})"

            lines[key] = {
                "page": page_number,
                "route": parsed["route"],
                "departures": parsed["departures"],
                "notes": parsed["notes"],
            }

    return {"kind": kind, "effective": effective, "lines": lines}


# ======================================================================
# Elle deneme:  python pdf_parser.py data/7-EKIM-3.pdf
# ======================================================================

if __name__ == "__main__":
    import sys

    for pdf_path in sys.argv[1:]:
        result = parse_pdf(pdf_path)
        print("=" * 70)
        print(pdf_path, "|", result["kind"], "| başlangıç:", result["effective"])
        print("=" * 70)
        for code, line in result["lines"].items():
            total = sum(len(v) for v in line["departures"].values())
            print(f"\n[{code}]  sayfa {line['page']}  toplam kalkış: {total}")
            for label, times in line["departures"].items():
                first = ", ".join(times[:3])
                last = ", ".join(times[-2:])
                print(f"   {label[:38]:38} {len(times):3d} sefer  ({first} ... {last})")
