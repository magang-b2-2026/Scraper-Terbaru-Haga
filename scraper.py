#!/usr/bin/env python3
"""
Web Content Scraper CLI - Clean Dataset Generator (untuk dataset CPT)
---------------------------------------------------------------------
Urutan ekstraksi per halaman (Tanpa Hardcoded Selectors/Words):
  1. Judul Artikel (Headline): diekstrak otomatis dari elemen <h1> utama di
     halaman (berdasarkan visibilitas & struktur DOM), bukan sekadar <title>.
  2. Isi Artikel:
     a. CSS path domain (sample_css_paths.csv) bila disediakan.
     b. Trafilatura pada HTML yang sudah dipangkas, lalu pada HTML mentah.
     c. Blok terpadat otomatis (menghitung rasio kepadatan teks non-link & <p>).
     d. CSS default "body" = pilihan TERAKHIR; hasilnya ditandai dan tidak masuk
        dataset kecuali --keep-body.

Pembersihan Otomatis (Berbasis Struktur & Statistik, Bukan Kosakata Hardcode):
  DOM  : buang tag non-teks standar HTML5, ukur kepadatan link hanya pada
         elemen BLOK (agar link inline seperti "(<a>Kemensos</a>)" tetap utuh),
         buang kontainer sisipan yang tidak memiliki kalimat utuh.
  Teks : satu baris = satu paragraf, pasang judul <h1> bersih di baris pertama
         (kecuali --drop-title), potong dateline pembuka secara struktural,
         buang baris non-kalimat / ALL-CAPS pendek, dan bersihkan boilerplate
         lintas dokumen secara otomatis.

Install:
  pip install playwright requests beautifulsoup4 markdownify trafilatura googlenewsdecoder
  playwright install chromium
"""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import types
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as html_to_md

# Shim kompatibilitas googlenewsdecoder dengan selectolax 1.0+
try:
    import selectolax.parser  # noqa: F401
except Exception:
    try:
        import selectolax.lexbor
        _m = types.ModuleType("selectolax.parser")
        _m.HTMLParser = selectolax.lexbor.LexborHTMLParser
        sys.modules["selectolax.parser"] = _m
    except Exception:
        pass

try:
    import trafilatura
except ImportError:
    trafilatura = None

# ================================================================ KONFIGURASI
DEFAULT_CSS = "body"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Spesifikasi W3C HTML5 untuk tag media/interaktif/form (bukan isi teks artikel)
HTML_NON_TEXT_TAGS = [
    "script", "style", "noscript", "iframe", "svg", "canvas",
    "video", "audio", "button", "input", "select", "textarea",
    "option", "figure", "figcaption", "dialog", "menu", "template"
]
# Tag kerangka halaman HTML5: di-unwrap bila membungkus artikel utama, dibuang bila kecil
HTML_LAYOUT_TAGS = ["nav", "footer", "aside", "header", "form"]

OPTS = {"dateline": True, "drop_title": False}

# Parameter numerik/struktural
LINK_DENSITY_MAX = 0.55
PROSE_MIN_WORDS = 6
DESCEND_RATIO = 0.55
GLOBAL_MIN_DOCS = 2
SHORT_LINE_WORDS = 8
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "source")
SKIP_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".zip", ".rar", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3")

# Pola sintaksis murni (tanpa daftar kata/frasa hardcode)
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_MD_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL_RE = re.compile(
    r"https?://\S+|www\.\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,}/\S*", re.I
)
QUOTE_RE = re.compile('[“”"«»„]')
ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# Pola struktural pemisah dateline di awal paragraf
_DL_SEP_RE = re.compile(
    r"\s*[–—]\s*"                        # en-dash / em-dash (termasuk 'VIVA –melainkan')
    r"|\s+-\s+"                          # hyphen dengan spasi di kedua sisi
    r"|\s+-(?=[A-Za-z])"                 # spasi lalu hyphen rapat ('VIVA -melainkan')
    r"|(?<=[A-Za-z0-9.)])-(?=[A-Z])"     # hyphen rapat sebelum huruf kapital ('Medan-Suasana')
    r"|:\s+"                             # titik dua diikuti spasi ('Jakarta: Kementerian')
)


# ============================================================= UTIL INPUT/URL
def read_lines(path: Optional[str]) -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def read_url_list(path: Optional[str]) -> List[str]:
    return [u for u in read_lines(path)
            if u.lower() not in ("url", "urls", "link", "links")]


def load_keywords(path: str) -> List[str]:
    return [k.lower() for k in read_lines(path)]


def clean_domain_key(raw: str) -> str:
    raw = raw.strip().strip('"')
    m = re.match(r"\[([^\]]*)\]\([^)]*\)", raw)
    if m:
        raw = m.group(1)
    raw = re.sub(r"^https?://", "", raw, flags=re.I)
    raw = re.sub(r"^www\.", "", raw, flags=re.I)
    return raw.rstrip("/").lower()


def load_css_paths(path: Optional[str]) -> Dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    mapping = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        f.seek(0)
        delim = ";" if ";" in first else ","
        for row in csv.DictReader(f, delimiter=delim):
            key = clean_domain_key(row.get("url") or row.get("domain") or "")
            css = (row.get("css_path") or row.get("csspath")
                   or row.get("css") or "").strip().strip('"')
            if key and css:
                mapping[key] = css
    return mapping


def host_of(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url).netloc.lower())


def site_of(url: str) -> str:
    labels = host_of(url).split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def get_css_for_url(url: str, css_map: Dict[str, str]) -> Tuple[Optional[str], str]:
    if not css_map:
        return None, "none"
    h = host_of(url)
    if h in css_map:
        return css_map[h], "exact"
    s = site_of(url)
    if s in css_map:
        return css_map[s], "site"
    parts = h.split(".")
    for i in range(len(parts) - 1):
        key = ".".join(parts[i:])
        if key in css_map:
            return css_map[key], "suffix"
    return None, "not_defined"


def host_in(url: str, domains: List[str]) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.lower().startswith(TRACKING_PARAMS)]
    return f"{host_of(url)}{p.path.rstrip('/')}?{urlencode(q)}".rstrip("?")


def split_selectors(sel: str) -> List[str]:
    parts, depth, cur = [], 0, ""
    for ch in sel:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return [p for p in parts if p]


# ====================================================== GOOGLE DORKING & RSS
def expand_queries(keywords: List[str], templates: List[str],
                   whens: List[str]) -> List[Dict]:
    out, seen = [], set()
    for kw in keywords:
        for tpl in templates:
            base = tpl.replace("{kw}", kw)
            for w in whens:
                wv = None if (w.lower() == "none" or "when:" in base.lower()) else w
                key = (base, wv)
                if key not in seen:
                    seen.add(key)
                    out.append({"q": base, "when": wv})
    return out


def gnews_query_text(item: Dict) -> str:
    return f'{item["q"]} when:{item["when"]}' if item["when"] else item["q"]


def gnews_rss_url(query: str, hl: str = "id", gl: str = "ID") -> str:
    return (f"https://news.google.com/rss/search?q={quote_plus(query)}"
            f"&hl={hl}&gl={gl}&ceid={gl}:{hl}")


def is_gnews(url: str) -> bool:
    return urlparse(url).netloc.endswith("news.google.com")


def decode_gnews(url: str) -> Optional[str]:
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        if isinstance(res, dict) and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception:
        pass
    return None


def fetch_gnews_items(query: str, hl: str, gl: str, limit: int) -> List[Dict]:
    try:
        r = requests.get(gnews_rss_url(query, hl, gl),
                         headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for it in root.iter("item"):
            link = it.findtext("link")
            if not link:
                continue
            src = it.find("source")
            src_url = src.get("url", "") if src is not None else ""
            items.append({"link": link, "source": host_of(src_url) if src_url else ""})
        return items[:limit]
    except Exception as e:
        print(f"    [!] Gagal ambil Google News RSS untuk {query!r}: {e}")
        return []


def pick_diverse(urls: List[str], source_of: Dict[str, str], limit: int) -> List[str]:
    if not limit or len(urls) <= limit:
        return urls
    groups: Dict[str, List[str]] = {}
    for u in urls:
        groups.setdefault(source_of.get(u) or host_of(u) or "?", []).append(u)
    picked: List[str] = []
    while len(picked) < limit and any(groups.values()):
        for g in list(groups):
            if groups[g] and len(picked) < limit:
                picked.append(groups[g].pop(0))
    return picked


# ====================================================== PEMBERSIHAN DOM OTOMATIS
def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True))


def _gone(el) -> bool:
    return bool(getattr(el, "decomposed", False))


ORPHAN_BLOCK_TAGS = {"div", "section", "ul", "ol", "li", "p", "h2", "h3", "h4"}


def _kill(el):
    """Buang elemen blok; bila induk bloknya tinggal label yatim pendek (<40 char), buang juga."""
    parent = el.parent
    el.decompose()
    for _ in range(3):
        if parent is None or parent.name in (None, "body", "html", "[document]"):
            break
        if parent.name in ORPHAN_BLOCK_TAGS and _text_len(parent) < 40:
            nxt = parent.parent
            parent.decompose()
            parent = nxt
        else:
            break


def prune_html(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Hapus tag media/script standar HTML5
    for t in soup(HTML_NON_TEXT_TAGS):
        t.decompose()
    for sm in soup.find_all("small"):
        if _text_len(sm) < 200:
            sm.decompose()

    total = max(1, _text_len(soup))

    # 2. Tag layout & tabel: bila membungkus mayoritas teks (>40%) -> unwrap,
    #    ATAU bila <header> berisi judul utama <h1> -> unwrap agar <h1> tidak hilang!
    for t in soup.find_all(HTML_LAYOUT_TAGS + ["table"]):
        if _gone(t):
            continue
        has_main_h1 = (t.name == "header" and t.find("h1") is not None
                       and _text_len(t.find("h1")) >= 15)
        if _text_len(t) > 0.4 * total or has_main_h1:
            for sub in t.find_all(["thead", "tbody", "tfoot", "tr", "td", "th"]):
                sub.unwrap()
            t.unwrap()
        else:
            t.decompose()

    # 3. Deteksi struktural HANYA pada elemen BLOK (div, section, ul, ol, li, p).
    #    Jangan masukkan 'span', 'a', 'strong', 'em' agar link inline di dalam
    #    kalimat seperti "Kementerian Sosial (<a>Kemensos</a>)" tidak ikut terhapus!
    for el in reversed(soup.find_all(["div", "ul", "ol", "section", "li", "p"])):
        if _gone(el):
            continue
        # Jangan pernah menghapus blok yang membungkus judul <h1>
        if el.find("h1") is not None:
            continue

        txt = el.get_text(" ", strip=True)
        text_total = len(txt)
        if text_total == 0:
            el.decompose()
            continue

        link_len = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
        if link_len > 0:
            nonlink = text_total - link_len
            if (link_len / text_total > LINK_DENSITY_MAX) or (nonlink <= 25 and text_total < 160):
                _kill(el)
                continue

        # Kontainer sisipan pendek (<120 char) yang tidak memiliki tanda baca kalimat (.!?)
        if el.name in ("div", "section", "li") and text_total < 120:
            if not re.search(r"[.!?…][\"”’)\]]*$", txt):
                _kill(el)
                continue

        # Paragraf/blok pendek (<120 char) dengan pola metadata "Label: Nilai" tanpa titik akhir kalimat
        if text_total < 120 and re.match(r"^[^.!?]{1,25}:\s+\S+", txt) and not re.search(r"[.!?…][\"”’)\]]*$", txt):
            _kill(el)

    # 4. Perbaiki drop-cap: <span>S</span>ebagai -> Sebagai
    for tag in soup.find_all(["span", "strong", "b", "i", "em", "font", "big", "a"]):
        t = tag.get_text(strip=True)
        nxt = tag.next_sibling
        if len(t) == 1 and t.isalpha() and isinstance(nxt, NavigableString) and nxt[:1].islower():
            tag.replace_with(t)

    # 5. Hapus tag gambar
    for img in soup.find_all("img"):
        img.decompose()
    return soup


def html_to_markdown(html: str) -> str:
    if not html or not html.strip():
        return ""
    soup = prune_html(html)
    return html_to_md(str(soup), heading_style="ATX", bullets="-",
                      strip=["a", "img"])


# ====================================================== PEMBERSIHAN TEKS OTOMATIS
def _strip_inline_md(s: str) -> str:
    """Bersihkan simbol bold/italic/code markdown & kurung kosong."""
    s = re.sub(r"\\([*_`~\[\]()#+\-.!])", r"\1", s)
    s = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", s)
    s = re.sub(r"(?<!\w)_{1,3}(.+?)_{1,3}(?!\w)", r"\1", s)
    s = s.replace("`", "")
    s = re.sub(r"\s*\(\s*\)", "", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _plain(line: str) -> str:
    s = re.sub(r"^[\s#>_\-*•]+|[\s_*]+$", "", line).strip()
    return _strip_inline_md(s)


def _norm(line: str, mask_digits: bool = True) -> str:
    n = re.sub(r"\s+", " ", _plain(line).lower())
    return re.sub(r"\d+", "#", n) if mask_digits else n


def is_prose(line: str) -> bool:
    s = _strip_inline_md(line.strip())
    return (len(s.split()) >= PROSE_MIN_WORDS
            and re.search(r"[.!?…][\"”’)\]]*$", s) is not None)


def _is_italic_para(line: str) -> bool:
    s = line.strip()
    if len(s) <= 2:
        return False
    return ((s.startswith("*") and s.endswith("*") and not s.startswith("**"))
            or (s.startswith("_") and s.endswith("_") and not s.startswith("__")))


def _is_label(line: str) -> bool:
    t = _plain(line)
    return (0 < len(t) <= 35 and len(t.split()) <= 4
            and not re.match(r"^[#>*\-•\d|]", line.strip())
            and not re.search(r"[.!?:;,]", t))


def cut_label_tail(line: str) -> str:
    """Potong ekor metadata yang menempel di akhir paragraf ('...pendidikan. Reporter : A')."""
    ends = list(re.finditer(r"[.!?…][\"”’)\]]*(?=\s|$)", line))
    if not ends:
        return line
    cut = ends[-1].end()
    tail = line[cut:].strip()
    if tail and len(tail) < 150 and ":" in tail and not re.search(r"[.!?…]", tail):
        return line[:cut]
    return line


def strip_dateline(line: str) -> str:
    """
    Potong dateline pembuka secara struktural tanpa daftar nama kota/media hardcode.
    """
    clean_line = _strip_inline_md(line)
    for m in _DL_SEP_RE.finditer(clean_line[:95]):
        pre = clean_line[:m.start()].strip()
        sep = m.group().strip()
        rest = clean_line[m.end():].strip()

        words = pre.split()
        if not (1 <= len(words) <= 7) or len(rest) < 25:
            continue

        has_domain = re.search(r"\b[a-z0-9-]+\.(com|id|co\.id|net|org|go\.id|tv|news)\b", pre, re.I) is not None
        if re.search(r"[!?]|\.\s+[A-Z]", pre) and not has_domain:
            continue

        alpha_only = re.sub(r"[^A-Za-z]", "", pre)
        is_all_caps = bool(alpha_only) and alpha_only.isupper()

        is_title_case = all(
            w.strip("(),.")[:1].isupper() or w.lower() in ("di", "dan", "of")
            for w in words if w.strip("(),.")
        )
        has_comma = "," in pre

        if not (is_all_caps or has_domain or is_title_case):
            continue

        if sep == ":":
            if len(words) > 3:
                continue
        elif sep == "-" and m.group() == "-":
            if not has_comma and len(words) < 2 and not is_all_caps and not has_domain:
                continue

        strong_signal = is_all_caps or has_comma or has_domain or sep in ("–", "—")
        if rest[:1].isupper() or rest[:1] in '“"(\'' or (strong_signal and rest[:1].islower()):
            return rest

    return line


def drop_line(s: str) -> bool:
    """Deteksi baris sampah/instruksi UI/metadata secara struktural tanpa kosakata hardcode."""
    if s.startswith("|"):
        return True
    if URL_RE.search(s):
        return True
    if not re.search(r"[A-Za-z]", s):
        return True

    # Jangan buang baris heading markdown (# Judul)
    if s.lstrip().startswith("#"):
        return False

    p = _plain(s)
    words = p.split()
    alpha = re.sub(r"[^A-Za-z]", "", p)
    is_list = re.match(r"^\s*(-|\*|\d+[.)]|[A-Z][.)])\s+", s) is not None
    ends_with_punct = re.search(r"[.!?…][\"”’)\]]*$", p) is not None

    # 1. Baris ALL-CAPS (seluruhnya huruf besar, mis. "GULIR UNTUK LANJUT BACA")
    #    yang tidak diakhiri tanda titik kalimat dan <= 10 kata
    if alpha and alpha.isupper() and len(words) <= 10 and not ends_with_punct:
        return True

    # 2. Pola atribut metadata pendek: "Label: Nilai" (<= 12 kata dan tidak diakhiri titik kalimat)
    #    Jika kata sebelum ':' pendek (1-2 kata, seperti 'Foto:', 'Editor:'), buang.
    #    Judul yang memakai ':' biasanya punya >2 kata atau huruf besar/kecil naratif.
    if len(words) <= 12 and re.match(r"^[A-Za-z\s/]{2,18}\s*:\s*\S+", p):
        label_part = p.split(":", 1)[0].strip()
        if len(label_part.split()) <= 2 and (len(words) <= 6 or not ends_with_punct):
            return True

    # 3. Pola kredit/atribut di ujung baris pendek, misal "... (Foto: Istimewa)"
    if len(p) < 260 and re.search(r"(\([A-Za-z\s]{2,15}:\s*[^)]+\)|\b[A-Za-z]{3,12}\s*:\s*\S+(?:\s+\S+){0,3})\.?\s*$", p):
        if not is_prose(p) or len(words) < 18:
            return True

    # 4. Baris pendek (<= 5 kata) yang bukan heading/list dan tidak memiliki tanda akhir kalimat (.!?)
    if len(words) <= 5 and not is_list and not ends_with_punct:
        return True

    return False


def cut_footer_run(lines: List[str]) -> List[str]:
    """Potong ekor artikel yang berisi deretan label pendek/tanda tangan penulis."""
    total = sum(len(l) for l in lines)
    if not total:
        return lines
    acc = 0
    for i, l in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        after = sum(len(x) for x in lines[i:])
        if _is_label(l):
            if nxt is not None and _is_label(nxt) and acc >= 0.5 * total and after <= max(0.3 * total, 220):
                return lines[:i]
            if acc >= 0.6 * total and after <= max(0.15 * total, 180) and len(lines) - i <= 3:
                return lines[:i]
        acc += len(l)
    return lines


def trim_to_prose_span(lines: List[str]) -> List[str]:
    idx = [i for i, l in enumerate(lines) if is_prose(l)]
    if not idx:
        return lines
    first, last = idx[0], idx[-1]
    head = None
    if not OPTS["drop_title"]:
        for j in range(first - 1, max(first - 7, -1), -1):
            if lines[j].lstrip().startswith("#"):
                head = lines[j]
                break
    return ([head] if head else []) + lines[first:last + 1]


def attach_headline(text: str, headline: str) -> str:
    """
    Pastikan judul artikel (<h1>) berada di baris paling atas artikel
    (kecuali --drop-title aktif), tanpa menduplikasi jika sudah ada.
    """
    if OPTS["drop_title"] or not headline or not text:
        return text
    clean_hl = _plain(headline)
    if len(clean_hl.split()) < 3:
        return text

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return text

    # Cek apakah baris ke-1 atau ke-2 sudah sama dengan judul
    norm_hl = re.sub(r"[^a-z0-9]", "", clean_hl.lower())
    for i in range(min(2, len(lines))):
        norm_line = re.sub(r"[^a-z0-9]", "", _plain(lines[i]).lower())
        if norm_hl and (norm_hl == norm_line or norm_hl in norm_line or norm_line in norm_hl):
            # Jika baris pertama sudah berupa judul, pastikan bentuknya bersih
            lines[i] = clean_hl
            return "\n\n".join(lines)

    return clean_hl + "\n\n" + "\n\n".join(lines)


def clean_markdown(md: str, headline: str = "") -> str:
    """Markdown mentah -> artikel bersih secara struktural."""
    if not md:
        return ""
    md = ZERO_WIDTH_RE.sub("", md).replace("\u00a0", " ")
    md = IMG_MD_RE.sub("", md)
    md = LINK_MD_RE.sub(r"\1", md)
    md = re.sub(r"([a-z0-9\)”\"’][.!?])([A-Z“])", r"\1 \2", md)

    lines = []
    for raw in md.split("\n"):
        s = raw.strip()
        if not s:
            continue
        s = cut_label_tail(s)
        if drop_line(s):
            continue
        lines.append(s)

    # Buang paragraf miring penuh di tepi atas/bawah (catatan redaksi/disclaimer)
    edge = set(range(min(3, len(lines)))) | set(range(max(0, len(lines) - 5), len(lines)))
    lines = [l for i, l in enumerate(lines) if not (i in edge and _is_italic_para(l))]

    lines = cut_footer_run(lines)
    lines = trim_to_prose_span(lines)

    seen, out = set(), []
    for l in lines:
        key = _norm(l, mask_digits=False)
        if len(key) > 20 and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(l)

    # Potong dateline pada awal artikel (cek hingga 2 paragraf prose pertama)
    if OPTS["dateline"]:
        prose_checked = 0
        for i, l in enumerate(out):
            if l.lstrip().startswith("#"):
                continue
            if is_prose(l) or i < 2:
                out[i] = strip_dateline(l)
                if is_prose(out[i]):
                    prose_checked += 1
                if prose_checked >= 2:
                    break

    res = "\n\n".join(out).strip()
    return attach_headline(res, headline)


def final_polish(text: str, headline: str = "", strip_quotes: bool = True) -> str:
    """Hasilkan teks polos bersih, pertahankan judul <h1> di baris pertama, & potong dateline."""
    out = []
    text = ZERO_WIDTH_RE.sub("", text or "").replace("\u00a0", " ")
    norm_hl = re.sub(r"[^a-z0-9]", "", _plain(headline).lower()) if headline else ""

    for idx, l in enumerate(text.split("\n")):
        l = l.strip()
        if not l:
            continue
        is_heading = l.lstrip().startswith("#")
        l = re.sub(r"^\s{0,3}#{1,6}\s*", "", l)
        l = re.sub(r"^\s*>\s?", "", l)
        l = _strip_inline_md(l)
        if strip_quotes:
            l = QUOTE_RE.sub("", l)
        l = re.sub(r"[ \t]{2,}", " ", l).strip()
        if not l:
            continue

        # Jangan buang baris pertama jika itu adalah judul artikel (headline)
        norm_l = re.sub(r"[^a-z0-9]", "", l.lower())
        is_title_line = is_heading or (idx == 0 and norm_hl and (norm_hl == norm_l or norm_hl in norm_l))
        if not is_title_line and drop_line(l):
            continue
        out.append(l)

    # Pastikan dateline pada paragraf pertama setelah judul ikut terpotong
    if OPTS["dateline"]:
        prose_checked = 0
        for i, l in enumerate(out):
            norm_l = re.sub(r"[^a-z0-9]", "", l.lower())
            if i == 0 and norm_hl and (norm_hl == norm_l or norm_hl in norm_l):
                continue  # Jangan potong judul artikel (misal 'Gus Ipul di ...: Wartawan ...')
            out[i] = strip_dateline(l)
            prose_checked += 1
            if prose_checked >= 2:
                break

    res = "\n\n".join(out).strip()
    return attach_headline(res, QUOTE_RE.sub("", headline) if strip_quotes else headline)


def quality_flags(text: str, headline: str = "") -> str:
    flags = []
    norm_hl = re.sub(r"[^a-z0-9]", "", _plain(headline).lower()) if headline else ""
    for l in [x.strip() for x in text.split("\n") if x.strip()][:3]:
        norm_l = re.sub(r"[^a-z0-9]", "", l.lower())
        if norm_hl and norm_l == norm_hl:
            continue
        if len(l) < 40:
            continue
        if l[:1].islower():
            flags.append("lowercase_start")
            break
    return ",".join(flags)


# ---- Pembersihan Lintas Dokumen: Baris Berulang = Boilerplate
def line_key(line: str) -> tuple:
    n = _norm(line)
    if ":" in n[:25]:
        label, rest = n.split(":", 1)
        if rest.strip() and len(label.split()) <= 3 and len(n) < 120:
            return ("L", label.strip())
    if len(n.split()) <= SHORT_LINE_WORDS:
        return ("S", n)
    return ("D", n)


def corpus_clean(results: List[Dict]) -> Tuple[int, int]:
    docs = [r for r in results if r["content"]]
    if len(docs) < 2:
        return 0, 0
    n_docs = len(docs)
    site_docs = Counter(site_of(r["final_url"]) for r in docs)
    global_counts: Counter = Counter()
    site_counts: Counter = Counter()
    for r in docs:
        site = site_of(r["final_url"])
        keys = {line_key(l) for l in r["content"].split("\n") if l.strip()}
        global_counts.update(keys)
        site_counts.update((site, k) for k in keys)

    def is_boiler(site: str, key: tuple) -> bool:
        if site_counts[(site, key)] >= max(2, math.ceil(0.5 * site_docs[site])):
            return True
        return key[0] in ("L", "S") and \
            global_counts[key] >= max(GLOBAL_MIN_DOCS, math.ceil(0.1 * n_docs))

    removed = protected = 0
    for r in docs:
        site = site_of(r["final_url"])
        kept, dropped = [], 0
        for l in r["content"].split("\n"):
            if l.strip() and is_boiler(site, line_key(l)):
                dropped += 1
                continue
            kept.append(l)
        cleaned = clean_markdown("\n".join(kept), headline=r.get("title", ""))
        if len(cleaned) < 0.3 * len(r["content"]):
            protected += 1
            continue
        removed += dropped
        r["content"] = cleaned
    return removed, protected


# ====================================================== EKSTRAKSI PER HALAMAN
# Ekstraksi Judul Artikel (Headline <h1>) secara otomatis dari DOM Visual + JSON-LD + Meta
EXTRACT_HEADLINE_JS = r"""
() => {
  // 1. Cari elemen <h1> di halaman yang bukan bagian dari nav/footer/aside
  const h1s = Array.from(document.querySelectorAll('h1'));
  let bestText = '';
  let bestScore = -1;

  for (const h of h1s) {
    if (h.closest('nav, footer, aside')) continue;
    const txt = (h.innerText || h.textContent || '').replace(/\s+/g, ' ').trim();
    if (txt.length < 15 || txt.split(' ').length < 3) continue;

    const rect = h.getBoundingClientRect();
    const style = window.getComputedStyle(h);
    if (style.display === 'none' || style.visibility === 'hidden') continue;

    const fontSize = parseFloat(style.fontSize) || 20;
    const inArticle = h.closest('article, main, [role="main"]') ? 1.5 : 1.0;
    // Skor gabungan: ukuran font + panjang judul wajar + posisi di dalam article/main
    const score = fontSize * inArticle * Math.min(txt.length, 140);
    if (score > bestScore) {
      bestScore = score;
      bestText = txt;
    }
  }
  if (bestText) return bestText;

  // 2. Cadangan: JSON-LD headline (standar Schema.org NewsArticle/Article)
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.textContent);
      const items = Array.isArray(data) ? data : (data['@graph'] || [data]);
      for (const it of items) {
        if (it && typeof it.headline === 'string' && it.headline.trim().length >= 15) {
          return it.headline.replace(/\s+/g, ' ').trim();
        }
      }
    } catch (e) {}
  }

  // 3. Cadangan: og:title / twitter:title (dipotong nama situs di ujungnya bila ada)
  const og = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
  if (og && og.content) {
    return og.content.replace(/\s+[-|–—]\s+[^-|–—]{2,35}$/, '').trim();
  }
  return '';
}
"""

EXTRACT_INNER_HTML_JS = r"""
(css) => {
  const els = Array.from(document.querySelectorAll(css));
  if (!els.length) return '';
  const top = els.filter(e => !els.some(o => o !== e && o.contains(e)));
  return top.map(e => e.innerHTML).join('\n\n');
}
"""

DENSE_BLOCK_JS = r"""
(ratio) => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','ASIDE','HEADER','IFRAME','SVG','FORM']);
  const score = e => {
    const t = (e.innerText || '').trim().length;
    if (!t) return 0;
    let l = 0;
    e.querySelectorAll('a').forEach(a => l += (a.innerText || '').trim().length);
    const pCount = e.querySelectorAll('p').length;
    const nonLink = Math.max(0, t - l);
    return nonLink * (1 + Math.min(pCount, 15) * 0.08);
  };
  let cur = document.body;
  while (cur && cur.children) {
    const cs = score(cur);
    let best = null, bs = 0;
    for (const k of cur.children) {
      if (skip.has(k.tagName.toUpperCase())) continue;
      const s = score(k);
      if (s > bs) { bs = s; best = k; }
    }
    if (best && bs >= ratio * cs) cur = best; else break;
  }
  return cur ? cur.innerHTML : '';
}
"""


def extract_headline(page) -> str:
    """Ambil judul artikel (<h1>) dari DOM, bukan sekadar <title> tab browser."""
    try:
        hl = page.evaluate(EXTRACT_HEADLINE_JS)
        if hl and isinstance(hl, str) and len(hl.strip()) >= 10:
            return _strip_inline_md(hl.strip())
    except Exception:
        pass
    # Fallback ke page.title() yang dibersihkan dari nama portal di belakang
    try:
        raw_title = (page.title() or "").strip()
        return re.sub(r"\s+[-|–—]\s+[^-|–—]{2,35}$", "", raw_title).strip()
    except Exception:
        return ""


def trafilatura_extract(html: str, url: str) -> str:
    if trafilatura is None or not html:
        return ""
    try:
        out = trafilatura.extract(
            html, url=url, output_format="markdown", include_links=False,
            include_images=False, include_tables=False, include_comments=False,
            favor_precision=True, deduplicate=True)
        return (out or "").strip()
    except Exception:
        return ""


def try_selectors(page, selector_list: str, min_chars: int, diag: Dict,
                  tag: str, headline: str = "") -> Tuple[str, str]:
    best = 0
    for sel in split_selectors(selector_list):
        try:
            html = page.evaluate(EXTRACT_INNER_HTML_JS, sel)
        except Exception:
            continue
        text = clean_markdown(html_to_markdown(html), headline=headline) if html else ""
        best = max(best, len(text))
        if len(text) >= min_chars:
            diag[tag] = len(text)
            return text, sel
    diag[tag] = best
    return "", ""


def extract_content(page, final_url: str, css_map: Dict[str, str],
                    default_css: str, min_chars: int,
                    diag: Dict, headline: str = "") -> Tuple[str, str]:
    # 1. CSS path domain (jika ada di CSV)
    css, src = get_css_for_url(final_url, css_map)
    if css:
        text, sel = try_selectors(page, css, min_chars, diag, "css", headline=headline)
        if text:
            return text, f"css:{sel} ({src})"

    # 2. Trafilatura: HTML terpangkas dulu, lalu mentah
    html = page.content()
    for label, h in (("pruned", None), ("raw", html)):
        if h is None:
            try:
                h = str(prune_html(html))
            except Exception:
                continue
        text = clean_markdown(trafilatura_extract(h, final_url), headline=headline)
        diag[f"traf_{label}"] = len(text)
        if len(text) >= min_chars:
            return text, f"trafilatura-{label}"

    # 3. Blok terpadat otomatis
    try:
        dense_html = page.evaluate(DENSE_BLOCK_JS, DESCEND_RATIO)
        text = clean_markdown(html_to_markdown(dense_html), headline=headline)
        diag["dense"] = len(text)
        if len(text) >= min_chars:
            return text, "auto-dense-block"
    except Exception:
        pass

    # 4. CSS default ("body"): pilihan terakhir
    text, sel = try_selectors(page, default_css or DEFAULT_CSS, min_chars, diag, "body", headline=headline)
    if text:
        return text, f"body-fallback:{sel}"
    return "", "failed"


def keywords_match(text: str, keywords: List[str]) -> bool:
    if not keywords:
        return True
    low = text.lower()
    return any(k in low for k in keywords)


def content_fingerprint(text: str) -> str:
    norm = re.sub(r"[^a-z0-9]", "", text.lower())[:800]
    return hashlib.sha1(norm.encode()).hexdigest()


# ================================================================= SCRAPING
def scrape_one(page, url: str, css_map: Dict[str, str], default_css: str,
               timeout: int, min_chars: int, exclude: List[str],
               keep_body: bool = False, debug_dir: Optional[str] = None) -> Dict:
    row = {"url": url, "final_url": url, "title": "", "method": "",
           "chars": "0", "status": "SUCCESS", "content": "", "raw": "",
           "flags": "", "diag": ""}
    try:
        target = (decode_gnews(url) or url) if is_gnews(url) else url
        page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)

        if is_gnews(page.url):
            ok = False
            for attempt in range(3):
                try:
                    page.wait_for_url(lambda u: not is_gnews(u), timeout=20000)
                    ok = True
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(3 * (attempt + 1))
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=timeout * 1000)
            if not ok:
                row["status"] = "GNEWS_REDIRECT_FAILED"
                return row

        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass

        row["final_url"] = page.url
        # Ekstrak judul <h1> artikel sebenarnya (bukan sekadar <title> browser)
        row["title"] = extract_headline(page)

        if exclude and host_in(page.url, exclude):
            row["status"] = "EXCLUDED_DOMAIN"
            return row

        diag: Dict[str, int] = {}
        content, method = extract_content(
            page, page.url, css_map, default_css, min_chars, diag, headline=row["title"]
        )
        row["method"] = method
        row["diag"] = "|".join(f"{k}:{v}" for k, v in diag.items())

        if method == "failed":
            best = max([v for k, v in diag.items() if k != "body"] or [0])
            row["status"] = "TOO_SHORT" if best >= 150 else "EXTRACTION_FAILED"
            content = ""
        elif method.startswith("body-fallback") and not keep_body:
            row["status"] = "LOW_QUALITY_BODY_FALLBACK"
            content = ""

        if debug_dir and row["status"] in ("TOO_SHORT", "EXTRACTION_FAILED",
                                           "LOW_QUALITY_BODY_FALLBACK"):
            os.makedirs(debug_dir, exist_ok=True)
            name = re.sub(r"[^a-z0-9]+", "_", host_of(page.url) + urlparse(page.url).path)[:80]
            with open(os.path.join(debug_dir, f"{name}.html"), "w",
                      encoding="utf-8") as f:
                f.write(page.content())

        row["content"] = content
        row["raw"] = content
    except Exception as e:
        row["status"] = "ERROR: " + (str(e).splitlines() or [type(e).__name__])[0][:140]
    return row


def scrape(urls: List[str], css_map: Dict[str, str], default_css: str,
           headless: bool = True, timeout: int = 30, min_chars: int = 300,
           exclude: Optional[List[str]] = None, keep_body: bool = False,
           debug_dir: Optional[str] = None,
           delay: Tuple[float, float] = (1.0, 2.0)) -> List[Dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[Error] pip install playwright && playwright install chromium")
        sys.exit(1)

    results: List[Dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, locale="id-ID")
        page = ctx.new_page()

        def run(url: str, idx: int, total: int, tag: str = "") -> Dict:
            print(f"[{idx}/{total}]{tag} {url[:100]}")
            row = scrape_one(page, url, css_map, default_css, timeout,
                             min_chars, exclude or [], keep_body, debug_dir)
            print(f"    -> {row['status']} | {row['method'][:60]} | "
                  f"{len(row['content'])} chars | {row['final_url'][:75]}")
            return row

        for idx, raw_url in enumerate(urls, 1):
            url = raw_url if raw_url.startswith(("http://", "https://")) \
                else f"https://{raw_url}"
            results.append(run(url, idx, len(urls)))
            time.sleep(random.uniform(*delay))

        failed = [i for i, r in enumerate(results)
                  if r["status"] == "GNEWS_REDIRECT_FAILED"]
        if failed:
            print(f"[*] Mengulang {len(failed)} link Google News yang gagal...")
            time.sleep(5)
            for i in failed:
                results[i] = run(results[i]["url"], i + 1, len(urls), " (ulang)")
                time.sleep(random.uniform(*delay))
        browser.close()
    return results


def finalize(results: List[Dict], keywords: List[str], min_chars: int,
             strip_quotes: bool = True):
    # 1. Saring duplikat
    seen_urls, seen_fp = set(), set()
    for r in results:
        if r["status"] != "SUCCESS" or not r["content"]:
            continue
        nu, fp = normalize_url(r["final_url"]), content_fingerprint(r["content"])
        if nu in seen_urls or fp in seen_fp:
            r["status"], r["content"] = "DUPLICATE", ""
            continue
        seen_urls.add(nu)
        seen_fp.add(fp)

    # 2. Pembersihan otomatis baris boilerplate lintas dokumen
    removed, protected = corpus_clean(results)
    print(f"[*] Pembersihan lintas dokumen: {removed} baris boilerplate dibuang"
          f"{f', {protected} dokumen dilindungi' if protected else ''}.")

    # 3. Poles teks polos (termasuk memasang judul <h1> di baris paling atas) & filter panjang + keyword
    for r in results:
        if r["status"] == "SUCCESS" and r["content"]:
            r["content"] = final_polish(r["content"], headline=r.get("title", ""), strip_quotes=strip_quotes)
    for r in results:
        if r["status"] != "SUCCESS":
            r["chars"] = "0"
            continue
        if len(r["content"]) < min_chars:
            r["status"], r["content"] = "TOO_SHORT", ""
        elif not keywords_match(r["content"], keywords):
            r["status"], r["content"] = "SKIPPED_NO_KEYWORD_MATCH", ""
        r["chars"] = str(len(r["content"]))
        r["flags"] = quality_flags(r["content"], headline=r.get("title", "")) if r["content"] else ""


# =============================================================== PENYIMPANAN
def ensure_parent(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def save_output(results: List[Dict], target: str, mode: str,
                md_headers: bool = False):
    valid = [r for r in results if r["content"].strip()]
    if mode == "merge":
        ensure_parent(target)
        blocks = [(f"# {r['final_url']}\n\n" if md_headers else "")
                  + r["content"].strip() for r in valid]
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(blocks) + "\n")
        print(f"\n[OK] {len(valid)} artikel bersih digabung ke: {os.path.abspath(target)}")
    else:
        os.makedirs(target, exist_ok=True)
        for i, r in enumerate(valid, 1):
            host = urlparse(r["final_url"]).netloc
            with open(os.path.join(target, f"article_{i:03d}_{host}.md"), "w",
                      encoding="utf-8") as f:
                f.write((f"# {r['final_url']}\n\n" if md_headers else "")
                        + r["content"].strip() + "\n")
        print(f"\n[OK] {len(valid)} file artikel disimpan di: {os.path.abspath(target)}")


def save_jsonl(results: List[Dict], path: str, field: str = "content"):
    rows = [r for r in results if r.get(field, "").strip()]
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "url": r["final_url"], "domain": host_of(r["final_url"]),
                "title": r["title"], "method": r["method"],
                "chars": len(r[field]), "flags": r.get("flags", ""),
                "text": r[field]}, ensure_ascii=False) + "\n")
    print(f"[OK] JSONL ({len(rows)} baris): {os.path.abspath(path)}")


def save_report(results: List[Dict], path: str):
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["url", "final_url", "title", "method", "chars", "status",
                    "flags", "diag"])
        for r in results:
            w.writerow([r["url"], r["final_url"], r["title"], r["method"],
                        r["chars"], r["status"], r.get("flags", ""),
                        r.get("diag", "")])
    n_flag = sum(1 for r in results if r.get("flags"))
    if n_flag:
        print(f"[!] {n_flag} artikel bertanda 'flags' -> cek kolom flags di laporan")
    print(f"[OK] Laporan status: {os.path.abspath(path)}")


# ======================================================================= CLI
def main():
    ap = argparse.ArgumentParser(description="Scraper artikel bersih untuk dataset CPT")
    ap.add_argument("--urls", "-u", default=None,
                    help="File daftar URL (default: sample_urls.txt bila tanpa --gnews/--dorks)")
    ap.add_argument("--keywords", default="kw_test.txt")
    ap.add_argument("--css-path", default=DEFAULT_CSS,
                    help="CSS default (pilihan terakhir). Default: 'body'")
    ap.add_argument("--css-paths", default="sample_css_paths.csv",
                    help="CSV mapping 'url;css_path' per domain")
    ap.add_argument("--output", "-o", default="test.md")
    ap.add_argument("--output-mode", choices=["each", "merge"], default="merge")
    ap.add_argument("--jsonl", default="dataset.jsonl")
    ap.add_argument("--raw-jsonl", default="raw_dataset.jsonl")
    ap.add_argument("--report", default="scrape_report.csv")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--min-chars", type=int, default=300)
    ap.add_argument("--exclude-domains", nargs="*", default=[])
    ap.add_argument("--keep-quotes", action="store_true",
                    help="Pertahankan tanda kutip (default: dihapus)")
    ap.add_argument("--md-headers", action="store_true",
                    help="Tambahkan '# URL' di tiap artikel pada output .md")
    ap.add_argument("--keep-body", action="store_true",
                    help="Ikutkan hasil fallback 'body' (tidak disarankan)")
    ap.add_argument("--keep-dateline", action="store_true",
                    help="Pertahankan dateline pembuka (mis. 'JAKARTA - '); default: dibuang")
    ap.add_argument("--drop-title", action="store_true",
                    help="Buang judul <h1> di awal artikel (default: judul <h1> disertakan di baris pertama)")
    ap.add_argument("--limit", type=int, default=30,
                    help="Maksimal URL yang di-scrape (default 30 untuk uji cepat; 0 = semua)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Seed pengacakan urutan query saat --limit dipakai")
    ap.add_argument("--debug-dir", default=None,
                    help="Simpan HTML halaman gagal/pendek ke folder ini untuk diperiksa")

    g = ap.add_argument_group("Pencarian URL (Google News RSS & dorking, Indonesia)")
    g.add_argument("--gnews", action="store_true")
    g.add_argument("--dorks", default=None, help="File template dork")
    g.add_argument("--when", nargs="+", default=["10d", "30d", "none"])
    g.add_argument("--hl", default="id")
    g.add_argument("--gl", default="ID")
    g.add_argument("--max-per-query", type=int, default=5)
    args = ap.parse_args()

    OPTS["dateline"] = not args.keep_dateline
    OPTS["drop_title"] = args.drop_title
    keywords = load_keywords(args.keywords)
    css_map = load_css_paths(args.css_paths)
    exclude = [clean_domain_key(d) for d in args.exclude_domains]
    print(f"[*] Default CSS: '{args.css_path}' | CSS mapping: {len(css_map)} domain "
          f"| keyword: {len(keywords)} | limit: {args.limit or 'semua'}")
    if trafilatura is None:
        print("[!] trafilatura belum terinstal -> kualitas turun. pip install trafilatura")

    use_search = bool(args.gnews or args.dorks)
    urls_file = args.urls or (None if use_search else "sample_urls.txt")
    source_of: Dict[str, str] = {}
    cands: List[str] = list(read_url_list(urls_file))

    if use_search:
        if not keywords:
            print(f"[Error] File keywords kosong/tidak ditemukan: {os.path.abspath(args.keywords)}")
            sys.exit(1)
        if args.dorks:
            if not os.path.isfile(args.dorks):
                print(f"[Error] File dorks tidak ditemukan: {os.path.abspath(args.dorks)}")
                sys.exit(1)
            templates = read_lines(args.dorks)
            if not templates:
                print(f"[Error] File dorks kosong: {os.path.abspath(args.dorks)}")
                sys.exit(1)
            print(f"[*] {len(templates)} template dork dimuat dari {args.dorks}")
        else:
            templates = ['"{kw}"']

        queries = expand_queries(keywords, templates, args.when)
        if args.limit:
            random.Random(args.seed).shuffle(queries)
        print(f"[*] {len(queries)} query | hl={args.hl}, gl={args.gl}")

        seen = set(cands)
        for item in queries:
            if args.limit and len(seen) >= args.limit * 3:
                print(f"[*] Kandidat sudah cukup ({len(seen)}), pencarian dihentikan.")
                break
            q = gnews_query_text(item)
            items = fetch_gnews_items(q, args.hl, args.gl, args.max_per_query)
            print(f"    [gnews] {q}  ->  {len(items)} link")
            for it in items:
                if it["link"] not in seen:
                    seen.add(it["link"])
                    cands.append(it["link"])
                    source_of[it["link"]] = it["source"]
            time.sleep(random.uniform(0.5, 1.2))

    cands = [u for u in dict.fromkeys(cands)
             if not urlparse(u).path.lower().endswith(SKIP_EXT)]
    urls = pick_diverse(cands, source_of, args.limit)
    if not urls:
        print("[!] Tidak ada URL valid untuk di-scrape.")
        sys.exit(0)
    if len(urls) < len(cands):
        print(f"[*] Mode uji: {len(urls)} dari {len(cands)} URL dipilih "
              f"(variasi antar-situs). Gunakan --limit 0 untuk semua.")

    print(f"[*] Memulai scraping {len(urls)} URL...")
    results = scrape(urls, css_map, args.css_path, headless=not args.no_headless,
                     timeout=args.timeout, min_chars=args.min_chars,
                     exclude=exclude, keep_body=args.keep_body,
                     debug_dir=args.debug_dir)

    if args.raw_jsonl:
        save_jsonl(results, args.raw_jsonl, field="raw")
    finalize(results, keywords, args.min_chars, strip_quotes=not args.keep_quotes)
    save_output(results, args.output, args.output_mode, args.md_headers)
    if args.jsonl:
        save_jsonl(results, args.jsonl, field="content")
    save_report(results, args.report)

    c = Counter(r["status"].split(":")[0] for r in results)
    print("[*] Ringkasan status: " + ", ".join(f"{k}={v}" for k, v in c.most_common()))


if __name__ == "__main__":
    main()