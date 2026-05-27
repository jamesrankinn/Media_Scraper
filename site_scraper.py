#!/usr/bin/env python3
"""
Async media scraper — Playwright rendering + deep extraction + aiohttp downloads.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import re
import random
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image, ImageFilter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TransferSpeedColumn,
)

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".svg", ".mp4", ".webm",
    ".gif", ".webp", ".avif",
}

# Extensions eligible for enhancement / conversion
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".avif"}
VIDEO_EXTS = {".mp4", ".webm"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 2560, "height": 1440},
]

# ── Logging ────────────────────────────────────────────────────────────────────

console = Console()


def setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
    )
    # Silence noisy third-party loggers
    for noisy in ("asyncio", "playwright", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("scraper")


# ── URL helpers ────────────────────────────────────────────────────────────────

def _norm_url(url: str) -> str:
    """Normalize a URL for deduplication (drop fragment, trailing slash)."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    normalized = f"{p.scheme}://{p.netloc}{path}"
    if p.query:
        normalized += f"?{p.query}"
    return normalized


def best_from_srcset(srcset: str, base_url: str) -> Optional[str]:
    """Return the highest-resolution URL from a srcset string."""
    candidates: list[tuple[int, str]] = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        url = tokens[0]
        if len(tokens) >= 2:
            try:
                width = int(tokens[1].rstrip("wx"))
            except ValueError:
                width = 0
        else:
            width = 1
        candidates.append((width, urljoin(base_url, url)))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def is_valid_media_url(url: str) -> bool:
    path = urlparse(url).path.lower().split("?")[0]
    return any(path.endswith(ext) for ext in VALID_EXTENSIONS)


def url_to_filename(url: str) -> str:
    path = urlparse(url).path
    name = os.path.basename(path)
    if not name or "." not in name:
        # Hash the URL so we still save the file rather than silently skip it
        name = hashlib.md5(url.encode()).hexdigest()[:12] + ".bin"
    return name


# ── Page scraping (Playwright) ─────────────────────────────────────────────────

async def scroll_to_bottom(page, log: logging.Logger) -> None:
    """Scroll incrementally so lazy-loaded elements fire their load events."""
    prev_height = 0
    for _ in range(25):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        await asyncio.sleep(0.35)
        height = await page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height
    log.debug("Scroll complete")


async def extract_page_links(page, base_url: str, log: logging.Logger) -> set[str]:
    """Return same-domain internal links found on the page."""
    base_netloc = urlparse(base_url).netloc
    hrefs = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
    """)
    links: set[str] = set()
    skip_exts = {".pdf", ".zip", ".mp3", ".dmg", ".exe", ".doc", ".docx", ".xls", ".xlsx"}
    for href in hrefs:
        if not href:
            continue
        href = href.split("#")[0]
        if not href:
            continue
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc != base_netloc:
            continue
        if any(parsed.path.lower().endswith(ext) for ext in skip_exts):
            continue
        links.add(href)
    log.debug(f"Found {len(links)} internal link(s) on {base_url}")
    return links


async def extract_media_urls(page, base_url: str, log: logging.Logger) -> set[str]:
    """Deep extraction: img, picture/source srcset, video, CSS background-image."""
    urls: set[str] = set()

    # 1. <img src>, <img srcset>, data-src (common lazy-load pattern)
    img_data = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('img')).map(el => ({
            src:     el.src || '',
            srcset:  el.getAttribute('srcset') || el.getAttribute('data-srcset') || '',
            dataSrc: el.getAttribute('data-src') || '',
        }))
    """)
    for item in img_data:
        for src in (item["src"], item["dataSrc"]):
            if src and is_valid_media_url(src):
                urls.add(src)
        if item["srcset"]:
            best = best_from_srcset(item["srcset"], base_url)
            if best and is_valid_media_url(best):
                urls.add(best)

    # 2. <picture><source srcset> — highest-res variant per source
    picture_data = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('picture source')).map(el => ({
            srcset: el.getAttribute('srcset') || '',
        }))
    """)
    for item in picture_data:
        if item["srcset"]:
            best = best_from_srcset(item["srcset"], base_url)
            if best and is_valid_media_url(best):
                urls.add(best)

    # 3. <video src> and <source src> inside video elements
    video_srcs = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('video').forEach(v => { if (v.src) out.push(v.src); });
        document.querySelectorAll('video source, source').forEach(s => { if (s.src) out.push(s.src); });
        return out;
    }""")
    for src in video_srcs:
        if src and is_valid_media_url(src):
            urls.add(src)

    # 4. Inline CSS background-image on every element
    bg_urls = await page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('*')) {
            const bg = window.getComputedStyle(el).backgroundImage;
            if (bg && bg !== 'none') {
                const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/i);
                if (m) out.push(m[1]);
            }
        }
        return out;
    }""")
    for raw in bg_urls:
        full = urljoin(base_url, raw)
        if is_valid_media_url(full):
            urls.add(full)

    log.debug(f"Extracted {len(urls)} unique media URLs from {base_url}")
    return urls


async def scrape_page(
    url: str,
    browser,
    sem: asyncio.Semaphore,
    log: logging.Logger,
) -> tuple[str, set[str], set[str]]:
    """Render a page with Playwright and return (url, media_urls, internal_links)."""
    user_agent = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORTS)

    async with sem:
        context = await browser.new_context(
            user_agent=user_agent,
            viewport=viewport,
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        # Drop font/analytics requests to speed up load
        await page.route(
            re.compile(r"\.(woff2?|ttf|eot|otf)(\?.*)?$", re.I),
            lambda route: route.abort(),
        )

        try:
            log.info(f"Loading [bold]{url}[/bold]")
            await page.goto(url, wait_until="networkidle", timeout=35_000)
            await scroll_to_bottom(page, log)
            # Second settle after scroll-triggered requests
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            log.warning(f"[yellow]Timeout[/yellow] on {url} — extracting partial results")
        except Exception as exc:
            log.error(f"[red]Failed to load[/red] {url}: {exc}")
            await context.close()
            return url, set(), set()

        media_urls = await extract_media_urls(page, url, log)
        page_links = await extract_page_links(page, url, log)
        await context.close()
        return url, media_urls, page_links


# ── Downloading (aiohttp) ──────────────────────────────────────────────────────

async def download_file(
    url: str,
    dest_dir: Path,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    progress: Progress,
    task_id,
    log: logging.Logger,
) -> bool:
    filename = url_to_filename(url)
    dest = dest_dir / filename

    if dest.exists():
        log.debug(f"Skip (exists): {filename}")
        progress.advance(task_id)
        return True

    async with sem:
        for attempt in range(3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 403:
                        log.warning(f"[yellow]403 Forbidden[/yellow]: {url}")
                        progress.advance(task_id)
                        return False
                    resp.raise_for_status()
                    data = await resp.read()
                dest.write_bytes(data)
                log.debug(f"Saved {filename} ({len(data):,} bytes)")
                progress.advance(task_id)
                return True
            except asyncio.TimeoutError:
                log.warning(f"Timeout on attempt {attempt + 1}/3: {url}")
            except aiohttp.ClientError as exc:
                log.warning(f"Download error attempt {attempt + 1}/3: {exc}")
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))

    log.error(f"[red]Gave up[/red] after 3 attempts: {url}")
    progress.advance(task_id)
    return False


# ── Enhancement ───────────────────────────────────────────────────────────────

def _enhance_image(src: Path) -> bool:
    """Sharpen and convert one image to .webp; return True on success."""
    try:
        with Image.open(src) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=2))
            dest = src.with_suffix(".webp")
            img.save(dest, "WEBP", quality=90, method=6)
        src.unlink()
        return True
    except Exception:
        return False


async def _enhance_video(src: Path) -> bool:
    """Re-encode a video with ffmpeg: unsharp + CRF 18; return True on success."""
    tmp = src.with_name(src.stem + "_enhanced" + src.suffix)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", "unsharp=5:5:1.0:5:5:0.0",
        "-crf", "18", "-preset", "slow",
        "-movflags", "+faststart",
        str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0 and tmp.exists():
        src.unlink()
        tmp.rename(src)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


async def enhance_all(output_dir: Path, log: logging.Logger) -> None:
    """Phase 3: enhance quality and convert images to .webp; re-encode videos."""
    loop = asyncio.get_event_loop()

    all_files = [f for f in output_dir.rglob("*") if f.is_file()]
    images = [f for f in all_files if f.suffix.lower() in IMAGE_EXTS]
    videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXTS]

    if not images and not videos:
        log.info("No files to enhance.")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        if images:
            img_task = progress.add_task(f"Enhancing {len(images)} image(s) → .webp", total=len(images))
            img_results = []
            for img_path in images:
                ok = await loop.run_in_executor(None, _enhance_image, img_path)
                img_results.append(ok)
                progress.advance(img_task)
            log.info(f"Images: {sum(img_results)}/{len(images)} converted to .webp")

        if videos:
            vid_task = progress.add_task(f"Re-encoding {len(videos)} video(s)", total=len(videos))
            vid_coros = [_enhance_video(v) for v in videos]
            vid_results = []
            for fut in asyncio.as_completed(vid_coros):
                ok = await fut
                vid_results.append(ok)
                progress.advance(vid_task)
            log.info(f"Videos: {sum(vid_results)}/{len(videos)} re-encoded")


# ── Orchestration ──────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    log = setup_logging(args.verbose)

    url_file = Path(args.url_file)
    if not url_file.exists():
        log.error(f"URL file not found: {url_file}")
        sys.exit(1)

    urls: list[str] = []
    for raw in url_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("http"):
            line = "https://" + line
        urls.append(line)

    if not urls:
        log.error("No URLs found in file.")
        sys.exit(1)

    log.info(f"Found [bold]{len(urls)}[/bold] URL(s) to scrape")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    page_sem = asyncio.Semaphore(args.pages)
    dl_sem = asyncio.Semaphore(args.downloads)

    # ── Phase 1: Crawl + Scrape (BFS per seed URL) ────────────────────────────
    scraped: list[tuple[str, str, set[str]]] = []  # (page_url, domain, media_urls)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            crawl_task = progress.add_task("Crawling & scraping", total=None)

            for seed_url in urls:
                seed_netloc = urlparse(seed_url).netloc
                visited: set[str] = {_norm_url(seed_url)}
                current_level: list[str] = [seed_url]
                pages_scraped = 0

                for depth in range(args.depth + 1):
                    if not current_level or pages_scraped >= args.max_pages:
                        break

                    # Respect max_pages cap
                    current_level = current_level[: args.max_pages - pages_scraped]

                    results = await asyncio.gather(*[
                        scrape_page(u, browser, page_sem, log)
                        for u in current_level
                    ])

                    next_level: list[str] = []
                    for page_url, media_urls, page_links in results:
                        domain = urlparse(page_url).netloc
                        scraped.append((page_url, domain, media_urls))
                        pages_scraped += 1
                        log.info(
                            f"[green]{domain}[/green]: {len(media_urls)} asset(s) — {page_url}"
                        )
                        progress.advance(crawl_task)

                        if depth < args.depth:
                            for link in page_links:
                                # Stay on same domain
                                if urlparse(link).netloc != seed_netloc:
                                    continue
                                norm = _norm_url(link)
                                if norm not in visited:
                                    visited.add(norm)
                                    next_level.append(link)

                    current_level = next_level

                log.info(
                    f"[bold]{seed_netloc}[/bold]: crawled {pages_scraped} page(s) "
                    f"({depth} depth level(s))"
                )

        await browser.close()

    # ── Phase 2: Download assets ───────────────────────────────────────────────
    dl_headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold green]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            # Group media URLs by domain so each domain gets one progress bar
            domain_assets: dict[str, set[str]] = {}
            for page_url, domain, media_urls in scraped:
                domain_assets.setdefault(domain, set()).update(media_urls)

            dl_coros = []
            for domain, asset_urls in domain_assets.items():
                site_dir = output_dir / domain
                site_dir.mkdir(parents=True, exist_ok=True)
                dl_task = progress.add_task(f"↓ {domain}", total=max(len(asset_urls), 1))
                for asset_url in asset_urls:
                    dl_coros.append(
                        download_file(asset_url, site_dir, session, dl_sem, progress, dl_task, log)
                    )

            results = await asyncio.gather(*dl_coros)

    ok = sum(results)
    total = len(results)
    log.info(f"{ok}/{total} file(s) downloaded")

    # ── Phase 3: Enhance + convert ─────────────────────────────────────────────
    if not args.no_enhance:
        console.rule("[bold magenta]Phase 3: Enhance & Convert[/bold magenta]")
        await enhance_all(output_dir, log)

    console.rule()
    console.print(
        f"[bold]Complete.[/bold] {ok}/{total} file(s) downloaded to "
        f"[bold]{output_dir.resolve()}[/bold]"
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Async media scraper: Playwright rendering + deep extraction + aiohttp downloads",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "url_file",
        nargs="?",
        default="target_sites.txt",
        help="Path to a text file with one URL (or bare domain) per line",
    )
    parser.add_argument(
        "-o", "--output",
        default="Scraped_Media",
        metavar="DIR",
        help="Root directory for downloaded files",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        metavar="N",
        help="Max concurrent Playwright browser pages",
    )
    parser.add_argument(
        "--downloads",
        type=int,
        default=10,
        metavar="N",
        help="Max concurrent file downloads",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        metavar="N",
        help="How many link-levels deep to crawl from each seed URL (0 = home page only)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        metavar="N",
        help="Max pages to crawl per seed URL",
    )
    parser.add_argument(
        "--no-enhance",
        action="store_true",
        help="Skip Phase 3 quality enhancement and .webp conversion",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
