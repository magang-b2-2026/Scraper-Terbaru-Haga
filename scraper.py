#!/usr/bin/env python3
"""
Web Content Scraper CLI - mode OTOMATIS (untuk dataset CPT)
-----------------------------------------------------------
Tidak ada daftar frasa/selector sampah buatan tangan. Pembersihan memakai
sinyal struktur & statistik:

  Ekstraksi (per halaman)
    1. Trafilatura (algoritma main-content, precision lalu recall)
    2. Blok terpadat otomatis: turun ke anak elemen yang memuat mayoritas
       teks non-link (tanpa selector apa pun)
    3. "body" hanya pilihan terakhir -> tidak masuk dataset
    (opsional) --css-paths: override manual per domain, bila ingin

  Pembersihan per dokumen
    - link density: blok yang isinya hampir semua link dibuang
    - span prosa: potong awal & akhir yang bukan kalimat (menu, tanggal,
      kategori, kredit, daftar "berita lainnya")
  Pembersihan lintas dokumen (data-driven)
    - baris yang berulang di banyak halaman = boilerplate (footer, hak cipta,
      "Editor : ...", "Advertisement", dst) -> dibuang otomatis

Install:
  pip install playwright requests beautifulsoup4 markdownify trafilatura
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
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as html_to_md

try:
    import trafilatura
except ImportError:
    trafilatura = None

# ---------------------------------------------------------------- konfigurasi
DEFAULT_CSS = "body"  # pilihan terakhir

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Tag HTML standar yang memang bukan isi artikel (bukan daftar situs tertentu)
NON_CONTENT_TAGS = ["script", "style", "noscript", "iframe", "svg", "nav",
                    "footer", "aside", "form", "button", "figcaption"]

# Ambang statistik (angka, bukan frasa)
LINK_DENSITY_MAX = 0.6     # blok dengan >60% teks berupa link dibuang
PROSE_MIN_WORDS = 6        # kalimat = >=6 kata dan diakhiri tanda baca
DESCEND_RATIO = 0.6        # turun ke anak yang memuat >=60% teks non-link
GLOBAL_MIN_DOCS = 3        # baris pendek/label berulang di >=3 dokumen
SHORT_LINE_WORDS = 8       # "baris pendek" = <=8 kata
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "source")


# ------------------------------------------------------------- util input
def read_lines(path: str) -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def read_url_list(path: str) -> List[str]:
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
    """Opsional: override manual per domain (CSV ';' url;css_path)."""
    if not path or not os.path.isfile(path):
        return {}
    mapping = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            key = clean_domain_key(row.get("url", ""))
            css = (row.get("css_path") or "").strip().strip('"')
            if key and css:
                mapping[key] = css
    return mapping


def host_of(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url).netloc.lower())


def site_of(url: str) -> str:
    """Domain induk: news.detik.com -> detik.com, bekasikab.go.id tetap utuh."""
    labels = host_of(url).split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def get_css_for_url(url: str, css_map: Dict[str, str]) -> Optional[str]:
    parts = host_of(url).split(".")
    for i in range(len(parts) - 1):
        key = ".".join(parts[i:])
        if key in css_map:
            return css_map[key]
    return None


def host_in(url: str, domains: List[str]) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.lower().startswith(TRACKING_PARAMS)]
    return f"{host_of(url)}{p.path.rstrip('/')}?{urlencode(q)}".rstrip("?")


# -------------------------------------------------------- Google News RSS
SKIP_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".zip", ".rar", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3")


def expand_queries(keywords: List[str], templates: List[str],
                   whens: List[str]) -> List[Dict]:
    """
    keyword x template dork x kondisi waktu.
      template : '"{kw}" site:go.id'   ({kw} diganti keyword)
      when     : '10d' | '30d' | 'none'  (none = tanpa batas waktu)
    Template yang sudah memuat 'when:' dipakai apa adanya.
    """
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
    """
    Dekode link Google News ke URL asli TANPA browser (lebih stabil daripada
    menunggu redirect JS). Opsional: pip install googlenewsdecoder.
    Mengembalikan None bila library tidak ada / gagal -> pakai redirect browser.
    """
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        return None
    try:
        res = gnewsdecoder(url, interval=1)
        if isinstance(res, dict) and res.get("status") and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception:
        pass
    return None


def fetch_gnews_links(query: str, hl: str, gl: str, limit: int) -> List[str]:
    try:
        r = requests.get(gnews_rss_url(query, hl, gl),
                         headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        links = [it.findtext("link") for it in root.iter("item")]
        return [l for l in links if l][:limit]
    except Exception as e:
        print(f"    [!] Gagal ambil RSS untuk {query!r}: {e}")
        return []


def fetch_brave_links(item: Dict, hl: str, gl: str, limit: int,
                      api_key: str) -> List[str]:
    """
    Pencarian web (termasuk situs non-berita) lewat Brave Search API.
    Mendukung operator site:/intitle:/inurl:. 'when' dipetakan ke 'freshness'
    berupa rentang tanggal. Perlu BRAVE_API_KEY (lihat brave.com/search/api).
    """
    params = {"q": item["q"], "country": gl.upper(), "search_lang": hl.lower(),
              "count": min(limit, 20)}
    m = re.fullmatch(r"(\d+)d", item["when"] or "")
    if m:
        end = date.today()
        params["freshness"] = f"{end - timedelta(days=int(m.group(1)))}to{end}"
    try:
        r = requests.get("https://api.search.brave.com/res/v1/web/search",
                         params=params, timeout=20,
                         headers={"X-Subscription-Token": api_key,
                                  "Accept": "application/json",
                                  "User-Agent": UA})
        r.raise_for_status()
        results = r.json().get("web", {}).get("results", [])
        return [x["url"] for x in results if x.get("url")][:limit]
    except Exception as e:
        print(f"    [!] Brave gagal untuk {item['q']!r}: {e}")
        return []


# ============================================================ HTML -> teks
def prune_html(html: str) -> BeautifulSoup:
    """Buang tag non-konten standar + blok berkepadatan link tinggi."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(NON_CONTENT_TAGS):
        t.decompose()

    # drop-cap: <span>S</span>ebagai -> Sebagai (huruf awal tidak boleh hilang)
    for tag in soup.find_all(["span", "strong", "b", "i", "em", "font", "big", "a"]):
        t = tag.get_text(strip=True)
        nxt = tag.next_sibling
        if len(t) == 1 and t.isalpha() and isinstance(nxt, NavigableString) \
                and nxt[:1].islower():
            tag.replace_with(t)

    # dari yang terdalam ke terluar
    for el in reversed(soup.find_all(["div", "ul", "ol", "section",
                                      "table", "li", "p"])):
        if getattr(el, "decomposed", False):
            continue
        total = len(el.get_text(" ", strip=True))
        if total == 0:
            el.decompose()
            continue
        link = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
        if link / total > LINK_DENSITY_MAX:
            el.decompose()
    return soup


def html_to_markdown(html: str) -> str:
    if not html or not html.strip():
        return ""
    soup = prune_html(html)
    return html_to_md(str(soup), heading_style="ATX", bullets="-",
                      strip=["a", "img"])


def extract_inner_html(page, css_selector: str) -> str:
    """innerHTML elemen yang cocok (tanpa elemen bersarang di elemen cocok lain)."""
    htmls = page.eval_on_selector_all(
        css_selector,
        """els => els
            .filter(e => !els.some(o => o !== e && o.contains(e)))
            .map(e => e.innerHTML)
            .filter(h => h && h.trim().length > 0)""",
    )
    return "\n\n".join(htmls)


# Tanpa selector: mulai dari <body>, turun ke anak yang memuat mayoritas teks
# non-link. Berhenti saat tidak ada anak yang dominan = kontainer artikel.
DENSE_BLOCK_JS = """
(ratio) => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','ASIDE','IFRAME','SVG','FORM']);
  const score = e => {
    const t = (e.innerText || '').trim().length;
    let l = 0;
    e.querySelectorAll('a').forEach(a => l += (a.innerText || '').trim().length);
    return t - l;  // teks non-link
  };
  let cur = document.body;
  while (true) {
    const cs = score(cur);
    let best = null, bs = 0;
    for (const k of cur.children) {
      if (skip.has(k.tagName.toUpperCase())) continue;
      const s = score(k);
      if (s > bs) { bs = s; best = k; }
    }
    if (best && bs >= ratio * cs) cur = best; else break;
  }
  return cur.innerHTML;
}
"""


def trafilatura_extract(html: str, url: str, precision: bool) -> str:
    if trafilatura is None or not html:
        return ""
    out = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=False,
        include_images=False, include_tables=False, include_comments=False,
        favor_precision=precision, favor_recall=not precision,
        deduplicate=True,
    )
    return (out or "").strip()


# ========================================================= pembersihan teks
def _plain(line: str) -> str:
    return re.sub(r"^[\s#>*_\-•]+|[\s*_]+$", "", line).strip()


def _norm(line: str, mask_digits: bool = True) -> str:
    """Normalisasi untuk membandingkan baris: huruf kecil, (opsional) angka -> #."""
    n = re.sub(r"\s+", " ", _plain(line).lower())
    return re.sub(r"\d+", "#", n) if mask_digits else n


def is_prose(line: str) -> bool:
    s = line.strip()
    return (len(s.split()) >= PROSE_MIN_WORDS
            and re.search(r"[.!?…][\"”’)\]]*$", s) is not None)


def cut_label_tail(line: str) -> str:
    """
    Potong ekor berlabel yang menempel di akhir kalimat tanpa tanda baca,
    mis. '...pendidikan.Reporter : A Editor : B'.
    """
    ends = list(re.finditer(r"[.!?…][\"”’)\]]*(?=\s|$)", line))
    if not ends:
        return line
    cut = ends[-1].end()
    tail = line[cut:].strip()
    if tail and len(tail) < 150 and ":" in tail \
            and not re.search(r"[.!?…]", tail):
        return line[:cut]
    return line


def trim_to_prose_span(lines: List[str]) -> List[str]:
    """Simpan dari kalimat pertama sampai kalimat terakhir (+ judul di atasnya)."""
    idx = [i for i, l in enumerate(lines) if is_prose(l)]
    if not idx:
        return lines
    first, last = idx[0], idx[-1]
    head, seen = None, 0
    for j in range(first - 1, -1, -1):
        if not lines[j].strip():
            continue
        seen += 1
        if lines[j].lstrip().startswith("#"):
            head = lines[j]
            break
        if seen >= 6:
            break
    return ([head, ""] if head else []) + lines[first:last + 1]


def structural_clean(text: str) -> str:
    """Pembersihan per dokumen (tanpa daftar frasa)."""
    if not text:
        return ""
    # kalimat yang menempel: 'perkembangan.Komitmen' -> 'perkembangan. Komitmen'
    text = re.sub(r"([a-z0-9\)”\"’][.!?])([A-Z“])", r"\1 \2", text)
    lines = [cut_label_tail(l.rstrip()) for l in text.split("\n")]
    lines = trim_to_prose_span(lines)

    seen, out = set(), []
    for l in lines:  # buang baris duplikat persis (judul 2x, dst)
        key = _norm(l, mask_digits=False)
        if len(key) > 20 and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(l)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---- pemoles akhir: teks polos, tanpa link/kutip/sisa footer
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL_RE = re.compile(
    r"https?://\S+|www\.\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*", re.I)
QUOTE_CHARS_RE = re.compile('[“”"«»„]')


def _is_italic_para(line: str) -> bool:
    s_ = line.strip()
    return (len(s_) > 2
            and re.match(r"^(\*(?!\*)|_(?!_))", s_) is not None
            and re.search(r"(?<!\*)\*$|(?<!_)_$", s_) is not None)


def _is_label(line: str) -> bool:
    """Label pendek polos (News, Travel): <=3 kata, tanpa tanda baca/bullet."""
    t = line.strip()
    return (0 < len(t) <= 30 and len(t.split()) <= 3
            and not re.match(r"^[#>*\-•\d]", t)
            and not re.search(r"[.!?:;,]", t))


def cut_footer_run(lines: List[str]) -> List[str]:
    """
    Potong footer di akhir artikel:
      - rangkaian >=2 label pendek berurutan (News, Travel) di paruh akhir
      - atau satu label pendek dengan sisa sangat kecil (<=15%, <=2 baris)
    Notifikasi/ajakan yang menyusul setelah label ikut terpotong.
    """
    idx = [i for i, l in enumerate(lines) if l.strip()]
    total = sum(len(lines[i]) for i in idx)
    if not total:
        return lines
    acc = 0
    for pos, i in enumerate(idx):
        nxt = idx[pos + 1] if pos + 1 < len(idx) else None
        after = sum(len(lines[j]) for j in idx[pos:])
        if _is_label(lines[i]):
            if nxt is not None and _is_label(lines[nxt]) \
                    and acc >= 0.5 * total and after <= 0.3 * total:
                return lines[:i]
            if acc >= 0.6 * total and after <= 0.15 * total \
                    and len(idx) - pos <= 3:
                return lines[:i]
        acc += len(lines[i])
    return lines


def final_polish(text: str, strip_quotes: bool = True) -> str:
    """Isi artikel saja: tanpa link, paragraf promo, footer, markup, kutip."""
    if not text:
        return ""
    lines = [MD_LINK_RE.sub(r"\1", l) for l in text.split("\n")]

    # paragraf yang seluruhnya miring di tepi dokumen = catatan/ajakan redaksi
    idx = [i for i, l in enumerate(lines) if l.strip()]
    edge = set(idx[:3] + idx[-5:])
    lines = ["" if (i in edge and _is_italic_para(l)) else l
             for i, l in enumerate(lines)]

    # baris yang memuat URL = promo/rujukan, bukan isi
    lines = ["" if URL_RE.search(l) else l for l in lines]

    lines = cut_footer_run(lines)
    lines = trim_to_prose_span(lines)

    out = []
    for l in lines:
        l = re.sub(r"^\s{0,3}#{1,6}\s*", "", l)                      # heading
        l = re.sub(r"^\s*>\s?", "", l)                               # blockquote
        l = re.sub(r"\*\*(.+?)\*\*", r"\1", l)                       # tebal
        l = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", l)
        l = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", l)  # miring
        l = l.replace("`", "")
        if strip_quotes:
            l = QUOTE_CHARS_RE.sub("", l)
        out.append(re.sub(r"[ \t]{2,}", " ", l).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def quality_flags(text: str) -> str:
    """Penanda mutu untuk ditinjau manual (tidak mengubah isi)."""
    flags = []
    lines = [l.strip() for l in text.split("\n") if l.strip()][:3]
    for l in lines:
        if len(l) < 40:
            continue
        m = re.match(r"^[^.!?]{0,80}?\s[-–—]\s+(?=\S)", l)
        rest = l[m.end():] if m else l
        if rest[:1].islower():
            flags.append("lowercase_start")  # mis. huruf awal drop-cap hilang
            break
    return ",".join(flags)


# ---- pembersihan lintas dokumen: baris yang berulang = boilerplate
def line_key(line: str) -> tuple:
    """
    Kunci kemiripan baris:
      L = 'label : isi'  (Editor : X, Foto : Y)  -> kunci = labelnya
      S = baris pendek (<=8 kata)                -> kunci = isi baris
      D = baris panjang                          -> kunci = isi baris
    """
    n = _norm(line)
    if ":" in n[:25]:
        label, rest = n.split(":", 1)
        if rest.strip() and len(label.split()) <= 3 and len(n) < 120:
            return ("L", label.strip())
    if len(n.split()) <= SHORT_LINE_WORDS:
        return ("S", n)
    return ("D", n)


def corpus_clean(results: List[Dict]) -> Tuple[int, int]:
    """
    Buang baris yang muncul di banyak dokumen:
      - per situs : ada di >=50% dokumen situs itu (minimal 2 dokumen)
      - global    : label/baris pendek ada di >=3 dokumen (atau 10% korpus)
    Mengembalikan (baris dibuang, dokumen yang dilindungi dari over-removal).
    """
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
        cleaned = structural_clean("\n".join(kept))
        # pengaman: bila >70% isi hilang, anggap over-removal -> pertahankan
        if len(cleaned) < 0.3 * len(r["content"]):
            protected += 1
            continue
        removed += dropped
        r["content"] = cleaned
    return removed, protected


# ====================================================== ekstraksi per halaman
def extract_content(page, final_url, css_map, min_chars) -> Tuple[str, str]:
    def finish(html_or_md: str, is_html: bool) -> str:
        md = html_to_markdown(html_or_md) if is_html else html_or_md
        return structural_clean(md)

    # 0. override manual (opsional)
    css = get_css_for_url(final_url, css_map) if css_map else None
    if css:
        try:
            text = finish(extract_inner_html(page, css), True)
            if len(text) >= min_chars:
                return text, f"css:{css}"
        except Exception:
            pass

    # 1. Trafilatura: precision dulu, lalu recall
    html = str(prune_html(page.content()))  # buang blok penuh-link (Baca juga, dst)
    for precision in (True, False):
        text = finish(trafilatura_extract(html, final_url, precision), False)
        if len(text) >= min_chars:
            return text, "trafilatura-" + ("precision" if precision else "recall")

    # 2. blok terpadat otomatis
    try:
        text = finish(page.evaluate(DENSE_BLOCK_JS, DESCEND_RATIO), True)
        if len(text) >= min_chars:
            return text, "auto-dense-block"
    except Exception:
        pass

    # 3. pilihan terakhir
    try:
        return finish(extract_inner_html(page, DEFAULT_CSS), True), "body-fallback"
    except Exception:
        return "", "body-fallback"


def keywords_match(text: str, keywords: List[str]) -> bool:
    if not keywords:
        return True
    low = text.lower()
    return any(k in low for k in keywords)


def content_fingerprint(text: str) -> str:
    norm = re.sub(r"[^a-z0-9]", "", text.lower())[:800]
    return hashlib.sha1(norm.encode()).hexdigest()


# ------------------------------------------------------------- scraping
def scrape_one(page, url, css_map, timeout, min_chars, exclude,
               keep_body) -> Dict:
    row = {"url": url, "final_url": url, "title": "", "method": "",
           "chars": "0", "status": "SUCCESS", "content": "", "raw": "",
           "flags": ""}
    try:
        target = decode_gnews(url) or url if is_gnews(url) else url
        page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)

        if is_gnews(target):  # tidak terdekode -> tunggu redirect lewat browser
            redirected = False
            for attempt in range(3):
                try:
                    page.wait_for_url(lambda u: not is_gnews(u), timeout=20000)
                    redirected = True
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(3 * (attempt + 1))  # jeda bertahap
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=timeout * 1000)
            if not redirected:
                row["status"] = "GNEWS_REDIRECT_FAILED"
                return row
            page.wait_for_load_state("domcontentloaded")

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        row["final_url"] = page.url
        row["title"] = (page.title() or "").strip()

        if exclude and host_in(page.url, exclude):
            row["status"] = "EXCLUDED_DOMAIN"
            return row

        content, method = extract_content(page, page.url, css_map, min_chars)
        row["method"] = method
        if method == "body-fallback" and not keep_body:
            row["status"] = "LOW_QUALITY_BODY_FALLBACK"
            content = ""
        row["content"] = content
        row["raw"] = content
    except Exception as e:
        row["status"] = f"ERROR: {e}"
    return row


def scrape(urls, css_map, headless=True, timeout=30, min_chars=300,
           exclude=None, keep_body=False, delay=(1.0, 2.5)) -> List[Dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[Error] pip install playwright && playwright install chromium")
        sys.exit(1)

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, locale="id-ID")
        page = ctx.new_page()

        for idx, raw_url in enumerate(urls, 1):
            url = raw_url if raw_url.startswith(("http://", "https://")) \
                else f"https://{raw_url}"
            print(f"[{idx}/{len(urls)}] {url[:110]}")
            row = scrape_one(page, url, css_map, timeout, min_chars,
                             exclude, keep_body)
            print(f"    -> {row['status']} | {row['method']} | "
                  f"{len(row['content'])} chars | {row['final_url'][:80]}")
            results.append(row)
            time.sleep(random.uniform(*delay))
        browser.close()
    return results


def finalize(results: List[Dict], keywords: List[str], min_chars: int,
             strip_quotes: bool = True):
    """Lintas dokumen: duplikat -> boilerplate -> panjang -> keyword."""
    # 1. duplikat dulu, agar salinan yang sama tidak dianggap bukti boilerplate
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

    # 2. baris yang berulang di banyak dokumen
    removed, protected = corpus_clean(results)
    print(f"[*] Pembersihan lintas dokumen: {removed} baris boilerplate dibuang"
          f"{f', {protected} dokumen dilindungi' if protected else ''}.")

    # 3. pemoles akhir (teks polos), lalu filter panjang & keyword
    for r in results:
        if r["status"] == "SUCCESS" and r["content"]:
            r["content"] = final_polish(r["content"], strip_quotes)
    for r in results:
        if r["status"] != "SUCCESS":
            r["chars"] = "0"
            continue
        if len(r["content"]) < min_chars:
            r["status"], r["content"] = "TOO_SHORT", ""
        elif not keywords_match(r["content"], keywords):
            r["status"], r["content"] = "SKIPPED_NO_KEYWORD_MATCH", ""
        r["chars"] = str(len(r["content"]))
        r["flags"] = quality_flags(r["content"]) if r["content"] else ""


# --------------------------------------------------------------- output
def ensure_parent(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def save_output(results, target: str, mode: str, md_headers: bool = False):
    valid = [r for r in results if r["content"].strip()]
    out_dir = os.path.dirname(target) if mode == "merge" else target
    os.makedirs(out_dir or ".", exist_ok=True)

    if mode == "merge":
        blocks = [(f"# {r['final_url']}\n\n" if md_headers else "")
                  + r["content"].strip() for r in valid]
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(blocks) + "\n")
        print(f"\n[OK] {len(valid)} artikel bersih digabung ke: "
              f"{os.path.abspath(target)}")
    else:
        for i, r in enumerate(valid, 1):
            host = urlparse(r["final_url"]).netloc
            path = os.path.join(out_dir, f"article_{i:03d}_{host}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write((f"# {r['final_url']}\n\n" if md_headers else "")
                        + r["content"].strip() + "\n")
        print(f"\n[OK] {len(valid)} file disimpan di: {os.path.abspath(out_dir)}")


def save_jsonl(results, path: str, field: str = "content"):
    rows = [r for r in results if r.get(field, "").strip()]
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "url": r["final_url"], "domain": host_of(r["final_url"]),
                "title": r["title"], "method": r["method"],
                "chars": len(r[field]), "flags": r.get("flags", ""),
                "text": r[field],
            }, ensure_ascii=False) + "\n")
    print(f"[OK] JSONL ({len(rows)} baris): {os.path.abspath(path)}")


def save_report(results, report_path: str):
    ensure_parent(report_path)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["url", "final_url", "method", "chars", "status", "flags"])
        for r in results:
            w.writerow([r["url"], r["final_url"], r["method"],
                        r["chars"], r["status"], r.get("flags", "")])
    n_flag = sum(1 for r in results if r.get("flags"))
    if n_flag:
        print(f"[!] {n_flag} artikel bertanda 'flags' -> cek kolom flags di laporan")
    print(f"[OK] Laporan status: {os.path.abspath(report_path)}")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Scraper artikel otomatis untuk dataset")
    ap.add_argument("--urls", "-u", default=None,
                    help="File daftar URL (default: sample_urls.txt, kecuali "
                         "bila --gnews dipakai)")
    ap.add_argument("--keywords", default="daftar_keywords.txt")
    ap.add_argument("--css-paths", default=None,
                    help="OPSIONAL: CSV ';' url;css_path untuk override manual")
    ap.add_argument("--output", "-o", default="output_articles.md")
    ap.add_argument("--output-mode", choices=["each", "merge"], default="merge")
    ap.add_argument("--jsonl", default="dataset.jsonl",
                    help="Dataset final (kosongkan '' untuk menonaktifkan)")
    ap.add_argument("--raw-jsonl", default="raw_dataset.jsonl",
                    help="Hasil sebelum pembersihan lintas dokumen")
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

    g = ap.add_argument_group("Pencarian URL (semua Indonesia)")
    g.add_argument("--gnews", action="store_true",
                   help="Cari URL lewat Google News RSS (tanpa API key)")
    g.add_argument("--dorks", default=None,
                   help="File template dork, mis. '\"{kw}\" site:go.id'")
    g.add_argument("--search-source", choices=["gnews", "brave", "both"],
                   default=None,
                   help="gnews=Google News RSS | brave=web (butuh BRAVE_API_KEY)")
    g.add_argument("--when", nargs="+", default=["10d", "30d", "none"])
    g.add_argument("--hl", default="id")
    g.add_argument("--gl", default="ID")
    g.add_argument("--max-per-query", type=int, default=10)
    args = ap.parse_args()

    keywords = load_keywords(args.keywords)
    css_map = load_css_paths(args.css_paths)
    exclude = [clean_domain_key(d) for d in args.exclude_domains]
    print(f"[*] Mode otomatis | override CSS: {len(css_map)} | "
          f"keyword: {len(keywords)}")
    if trafilatura is None:
        print("[!] trafilatura belum terinstal -> hanya 'auto-dense-block'. "
              "Disarankan: pip install trafilatura")

    use_search = bool(args.gnews or args.dorks or args.search_source)
    urls_file = args.urls or (None if use_search else "sample_urls.txt")
    urls = read_url_list(urls_file)

    if use_search:
        if not keywords:
            print(f"[Error] File keywords kosong/tidak ditemukan: "
                  f"{os.path.abspath(args.keywords)}")
            sys.exit(1)
        source = args.search_source or "gnews"
        if args.dorks:
            if not os.path.isfile(args.dorks):
                print(f"[Error] File dorks tidak ditemukan: "
                      f"{os.path.abspath(args.dorks)}")
                sys.exit(1)
            templates = read_lines(args.dorks)
            if not templates:
                print(f"[Error] File dorks kosong (atau semua barisnya diawali "
                      f"#): {os.path.abspath(args.dorks)}")
                sys.exit(1)
            print(f"[*] {len(templates)} template dork dimuat dari {args.dorks}")
        else:
            templates = ['"{kw}"']
        queries = expand_queries(keywords, templates, args.when)
        print(f"[*] {len(queries)} query | sumber={source} | "
              f"hl={args.hl}, gl={args.gl}")

        brave_key = os.environ.get("BRAVE_API_KEY", "")
        if source in ("brave", "both") and not brave_key:
            print("[!] BRAVE_API_KEY belum diset -> sumber brave dilewati "
                  "(PowerShell: $env:BRAVE_API_KEY='...')")

        for item in queries:
            if source in ("gnews", "both"):
                q = gnews_query_text(item)
                links = fetch_gnews_links(q, args.hl, args.gl, args.max_per_query)
                print(f"    [gnews] {q}  ->  {len(links)} link")
                urls.extend(links)
                time.sleep(random.uniform(1.0, 2.0))
            if source in ("brave", "both") and brave_key:
                links = fetch_brave_links(item, args.hl, args.gl,
                                          args.max_per_query, brave_key)
                tag = f' (when={item["when"]})' if item["when"] else ""
                print(f"    [brave] {item['q']}{tag}  ->  {len(links)} link")
                urls.extend(links)
                time.sleep(random.uniform(1.0, 2.0))

    before = len(urls)
    urls = [u for u in urls
            if not urlparse(u).path.lower().endswith(SKIP_EXT)]
    if len(urls) < before:
        print(f"[*] {before - len(urls)} URL file (pdf/doc/gambar/dll) dilewati.")

    urls = list(dict.fromkeys(urls))
    if not urls:
        print("[Error] Tidak ada URL untuk di-scrape.")
        sys.exit(1)

    results = scrape(urls, css_map, headless=not args.no_headless,
                     timeout=args.timeout, min_chars=args.min_chars,
                     exclude=exclude, keep_body=args.keep_body)

    if args.raw_jsonl:
        save_jsonl(results, args.raw_jsonl, field="raw")
    finalize(results, keywords, args.min_chars,
             strip_quotes=not args.keep_quotes)

    save_output(results, args.output, args.output_mode, args.md_headers)
    if args.jsonl:
        save_jsonl(results, args.jsonl, field="content")
    save_report(results, args.report)


if __name__ == "__main__":
    main()