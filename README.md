# Media Scraper

Async media scraper with Playwright-based JS rendering, deep asset extraction, and concurrent aiohttp downloads.

## What it extracts

| Source | Detail |
|---|---|
| `<img src>` | Direct image URLs |
| `<img srcset>` / `data-srcset` | Highest-resolution candidate from the srcset list |
| `<picture><source srcset>` | Highest-resolution candidate per `<source>` |
| `<video src>` / `<source src>` | Video file URLs |
| CSS `background-image` | Computed style on every element |

Supported formats: `.jpg` `.jpeg` `.png` `.webp` `.avif` `.svg` `.gif` `.mp4` `.webm`

---

## Installation

**Requirements: Python 3.9+**

### 1. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browsers

```bash
playwright install chromium
```

This downloads the Chromium binary Playwright uses (~150 MB). You only need to do this once.

---

## Usage

```
python site_scraper.py [url_file] [options]
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `url_file` | `target_sites.txt` | Text file with one URL or bare domain per line |
| `-o DIR` / `--output DIR` | `Scraped_Media` | Root output directory |
| `--pages N` | `3` | Max concurrent browser pages |
| `--downloads N` | `10` | Max concurrent file downloads |
| `-v` / `--verbose` | off | Show debug-level log output |

### Examples

```bash
# Scrape using the default target_sites.txt
python site_scraper.py

# Custom URL file and output directory
python site_scraper.py my_sites.txt -o /tmp/media

# Crank up concurrency on a fast connection
python site_scraper.py --pages 5 --downloads 20

# Debug mode to see every URL found and every file saved
python site_scraper.py -v
```

### URL file format

One entry per line. Bare domains are automatically prefixed with `https://`. Lines starting with `#` are ignored.

```
# Cannabis brands
hivecann.ca
https://earthtoskycannabis.com
claritycannabis.ca
```

---

## Output structure

Files are saved to a per-domain subfolder under the output directory:

```
Scraped_Media/
  hivecann.ca/
    hero-image.jpg
    product-photo.webp
    ...
  earthtoskycannabis.com/
    banner.jpg
    ...
```

Files that already exist are skipped (safe to re-run).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `playwright install` fails | Run `playwright install-deps chromium` on Linux |
| 403 errors on downloads | The site is blocking the downloader UA — the file is skipped and logged |
| Page loads partially | Increase `--pages` timeout by editing `timeout=35_000` in the source |
| Very slow on many sites | Increase `--pages` to open more browser tabs in parallel |
