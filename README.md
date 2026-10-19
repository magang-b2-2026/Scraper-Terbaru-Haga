# Web Content Scraper CLI (Clean Dataset Generator)

Script Python untuk melakukan scraping dan ekstraksi artikel web secara otomatis dari kata kunci maupun daftar URL, menghasilkan file **Markdown** dan **JSONL** yang bersih, rapi, dan siap digunakan langsung sebagai **dataset training CPT (Continual Pre-training)**.

---

## 7 Pemenuhan Syarat Proyek

| No | Syarat | Implementasi & Solusi |
|:---|:---|:---|
| **1** | **Default CSS path ke `"body"` (bukan `"body p"`)** | Parameter CLI `--css-path` kini ber-default `"body"`. Pada fallback terakhir di mana selector spesifik tidak tersedia, scraper mengekstrak kontainer `"body"` dan membersihkan seluruh elemen non-artikel. |
| **2** | **Definisikan sebanyak-banyaknya CSS path artikel** | File `sample_css_paths.csv` diperluas mencakup 30 portal berita nasional teratas (Detik, Kompas, Tempo, CNN, CNBC, dll.), portal pemerintahan (`.go.id`), portal akademik/kampus (`.ac.id`), blog publikasi (Medium, Kompasiana, Mojok, dll.), serta wildcard domain hierarchy. Di dalam kode juga ditambahkan *Smart Article Selectors* (`article`, `main`, `.entry-content`, `.post-content`, dll.) sebelum fallback ke `body`. |
| **3** | **Ubah dari get `innerText` ke `innerHTML`** | Ekstraksi pada engine Playwright diubah dari pembacaan `innerText` menjadi pengambilan `innerHTML` murni elemen. Hal ini mempertahankan struktur semantik dokumen (`<p>`, `<h1>`-`<h6>`, `<ul>`, `<li>`, `<strong>`, `<em>`) sebelum dibersihkan dan dikonversi ke Markdown. |
| **4** | **Google Dorking dan Google News RSS** | scraper mendukung input kata kunci (`--keywords`) yang digabungkan secara otomatis dengan template Google Dorking (`--dorks`) dan dieksekusi lewat Google News RSS Search (`https://news.google.com/rss/search?q=...&hl=id&gl=ID&ceid=ID:id`). Dilengkapi modul dekode instan URL Google News (`googlenewsdecoder`). |
| **5** | **Library Python konversi HTML ke Markdown** | Menggunakan library Python `markdownify` dikombinasikan dengan parser `BeautifulSoup` (bs4) untuk pembersihan DOM (pruning tag sampah, caption, rating, share widget) sebelum dikonversi ke Markdown yang terstruktur. |
| **6** | **Query multi-condition dari 30 CSS path (10d, 30d, all-time/none) lokasi Indonesia** | Disediakan file template `dorks_30_news.txt` dan `dorks_test.txt` yang memetakan keyword ke masing-masing 30 media dengan kondisi waktu multi-condition (`when:10d`, `when:30d`, dan tanpa when). Parameter lokasi disetel ke `hl=id` dan `gl=ID` (seluruhnya Indonesia). |
| **7** | **Mendukung artikel non-website berita** | Scraper telah diuji dan berhasil mengekstrak artikel dari website pemerintahan (`.go.id`), universitas/kampus (`.ac.id`), serta platform tulisan opini publikasi dengan tingkat keberhasilan dan kebersihan tinggi. |

---

## Solusi untuk 3 Temuan Kendala

### 1. Caption Foto & Widget CTA/Rating Masih Ikut Ter-scrape
- **Penyebab**: Beberapa website menaruh caption foto di tag `<small>`, `<figcaption>`, atau `<div class="caption">` (misal di `bekasikab.go.id`: *"Foto : Imanuddin/Diskominfosantik"*). Website lain menyertakan widget survei kepuasan pembaca seperti di `pu.go.id`: *"Apakah informasi di atas cukup membantu? YA TIDAK"*.
- **Solusi**:
  1. **DOM Pruning (`prune_html`)**: Menghapus seluruh tag `<small>`, `<figcaption>`, elemen berkelas `caption`, `image-caption`, `foto-caption`, `wp-caption`, `call-to-action`, `cta`, `rating`, `survey`, `social-share`, `social-icon`, dll.
  2. **Regex Filter (`strip_captions_and_cta`)**: Memfilter baris teks yang polanya merupakan atribusi gambar (`Foto : ...`, `(Foto: ...)`), serta mendeteksi dan membuang pertanyaan rating dan tombol responnya.

### 2. Antar-Paragraf Tidak Memiliki Pemisah 1 New Line
- **Penyebab**: Beberapa situs (seperti `bekasikab.go.id`) tidak membungkus paragraf dengan tag `<p>`, melainkan menaruh potongan teks kalimat secara langsung di dalam kontainer `div` dengan pemisah single newline (`\n`). Di Markdown, single newline menyebabkan paragraf menyatu tanpa jeda baris kosong.
- **Solusi**:
  - Diterapkan fungsi `format_paragraphs` yang menganalisis tanda baca akhir kalimat (`.`, `!`, `?`, `"`) dan awal kalimat baru (`[A-Z]`).
  - Seluruh paragraf dijamin dipisahkan oleh **persis 1 baris kosong (`\n\n`)** sesuai standar format Markdown dan dataset teks yang rapi.

### 3. Alur Scraping dari Keywords ke URL, Beberapa URL Tidak Ada di CSS Path
- **Penyebab**: Ketika mencari via kata kunci umum, Google mengembalikan beragam domain di luar 30 domain berita yang ada di CSV, sehingga URL tersebut tidak dikenali dan sebelumnya langsung didrop.
- **Solusi**:
  1. **Ekspansi CSV**: `sample_css_paths.csv` diperluas mencakup subdomain pemerintah (`.go.id`), kampus (`.ac.id`), dan blog.
  2. **Smart Hierarchical Extraction**:
     - *Level 1*: Cek CSS selector spesifik dari CSV.
     - *Level 2*: Jika tidak ada di CSV, coba *Smart Article Selectors* (`article, main, div.entry-content, div.post-content, div.detail-content, ...`).
     - *Level 3*: Algoritma artikel Trafilatura.
     - *Level 4*: Fallback `"body"` (Requirement 1) yang telah dibersihkan secara ketat.
  3. Hasilnya: tidak ada artikel valid yang terbuang sia-sia hanya karena domainnya belum terdaftar di CSV.

---

## Instalasi Dependencies

Pastikan menggunakan Python 3.10+ (atau virtual environment yang telah disediakan):

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Isi `requirements.txt`:
```txt
playwright
requests
beautifulsoup4
markdownify
trafilatura
googlenewsdecoder
```

---

## Panduan Penggunaan CLI

### 1. Eksekusi Pencarian Otomatis (Google Dorking + Google News RSS)
Mencari artikel berdasarkan kata kunci di `kw_test.txt` menggunakan template dork 30 media berita dan non-berita:
```bash
python scraper.py --dorks dorks_30_news.txt --output test.md --jsonl dataset.jsonl --report scrape_report.csv
```

### 2. Pengujian Cepat dengan Multi-Condition Waktu
```bash
python scraper.py --dorks dorks_test.txt --when 10d 30d none --hl id --gl ID --max-per-query 3 --output test.md
```

### 3. Eksekusi dari Daftar URL Manual
Jika sudah memiliki file daftar URL (misal `sample_urls.txt` atau `test_urls_verification.txt`):
```bash
python scraper.py --urls test_urls_verification.txt --output test.md --output-mode merge
```

### 4. Opsi Parameter Lengkap

| Parameter | Alias | Deskripsi | Default |
|:---|:---|:---|:---|
| `--urls` | `-u` | Path file daftar URL | `sample_urls.txt` |
| `--keywords` | - | File keyword pencarian | `kw_test.txt` |
| `--css-path` | - | Default CSS path fallback (**Requirement 1**) | `'body'` |
| `--css-paths` | - | CSV mapping URL ke CSS selector | `sample_css_paths.csv` |
| `--output` | `-o` | File output Markdown | `test.md` |
| `--output-mode` | - | Mode simpan: `merge` (gabung semua) atau `each` (1 file per artikel) | `merge` |
| `--jsonl` | - | File output JSONL dataset final | `dataset.jsonl` |
| `--raw-jsonl` | - | File output JSONL mentah sebelum pembersihan korpus | `raw_dataset.jsonl` |
| `--report` | - | File CSV laporan status scraping | `scrape_report.csv` |
| `--dorks` | - | File template dorking (`"{kw}" site:...`) | `None` |
| `--when` | - | Rentang waktu Google News (`10d`, `30d`, `none`) | `10d 30d none` |
| `--hl` | - | Bahasa Google News | `id` |
| `--gl` | - | Lokasi wilayah Google News | `ID` |
| `--max-per-query` | - | Batas link diambil per query | `5` |
| `--no-headless` | - | Tampilkan UI visual browser | `False` (Headless) |
| `--timeout` | - | Batas waktu loading halaman (detik) | `30` |
| `--min-chars` | - | Minimal panjang karakter artikel valid | `300` |

---

## File Hasil Output

- **`test.md`**: File Markdown bersih berisi teks murni hasil scraping, dipisahkan horizontal divider `\n\n---\n\n`, dengan pemisah 1 blank line (`\n\n`) di setiap paragraf.
- **`dataset.jsonl`**: Dataset terstruktur dalam format JSON Lines (`url`, `domain`, `title`, `method`, `chars`, `text`).
- **`scrape_report.csv`**: Laporan status scraping setiap URL (`url`, `final_url`, `method`, `chars`, `status`).
