#!/usr/bin/env python3
import argparse, os, re, sys, collections
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

MEGACOMBO_RE = re.compile(r"/learning/megacombo/[0-9a-fA-F-]{36}$")
PDF_RE       = re.compile(r"\.pdf($|\?)", re.IGNORECASE)

def ensure_dir(p): os.makedirs(p, exist_ok=True)
def sanitize(s):
    import re
    return re.sub(r'[\\/*?:"<>|\n\r\t]', "_", (s or "").strip())[:150] or "untitled"

def same_site(u, v):
    a, b = urlparse(u), urlparse(v)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)

def abs_links(page):
    hrefs = set()
    for a in page.locator("a[href]").all():
        try:
            h = a.get_attribute("href") or ""
        except Exception:
            continue
        if not h or h.startswith("#") or h.startswith("javascript:"):
            continue
        hrefs.add(urljoin(page.url, h))
    return sorted(hrefs)

def scroll(page, times=6, px=1400):
    for _ in range(times):
        page.mouse.wheel(0, px)
        page.wait_for_timeout(350)

def safe_goto(page, url, attempts=3):
    """Navigate with retries; handles transient net errors (e.g., ERR_NETWORK_IO_SUSPENDED)."""
    for i in range(1, attempts + 1):
        try:
            return page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            msg = str(e)
            if "ERR_NETWORK_IO_SUSPENDED" in msg or "net::ERR" in msg:
                print(f"  ! network error on goto (attempt {i}/{attempts}): {msg}")
                page.wait_for_timeout(1500 * i)  # simple backoff
                continue
            raise
    print("  ! giving up on this page due to repeated navigation errors")
    return None

def discover_megacombos(ctx, seeds, max_pages=80):
    """(Collect-first) Breadth-first discovery of all megacombo root pages (single level)."""
    found = set(); visited = set(); dq = collections.deque(seeds)
    while dq and len(visited) < max_pages:
        url = dq.popleft()
        if url in visited: continue
        visited.add(url)
        page = ctx.new_page(); page.set_default_timeout(15000)
        resp = safe_goto(page, url, attempts=3)
        if not resp:
            page.close(); continue
        scroll(page, 4)
        for u in abs_links(page):
            if not same_site(u, url): continue
            if MEGACOMBO_RE.search(urlparse(u).path): found.add(u)
            # BFS with a shallow depth limit to avoid crawling too deep
            p = urlparse(u)
            if p.path.count("/") <= 5 and not PDF_RE.search(u):
                dq.append(u)
        page.close()
    return sorted(found)

def stream_discover_and_download(ctx, seeds, out_dir, save_roots=None, max_pages=100, max_roots=500):
    """
    Streaming BFS: as soon as a megacombo root is discovered, download it immediately.
    Optionally append each found root to save_roots.
    """
    visited_pages = set()
    found_roots = set()
    dq = collections.deque(seeds)

    total_downloaded = 0
    pages_seen = 0

    while dq and pages_seen < max_pages and len(found_roots) < max_roots:
        url = dq.popleft()
        if url in visited_pages:
            continue
        visited_pages.add(url)

        page = ctx.new_page()
        page.set_default_timeout(15000)
        resp = safe_goto(page, url, attempts=3)
        if not resp:
            page.close()
            continue

        scroll(page, 4)

        for u in abs_links(page):
            if not same_site(u, url):
                continue
            p = urlparse(u)

            # If it's a megacombo root and not processed, process NOW
            if MEGACOMBO_RE.search(p.path):
                if u not in found_roots:
                    found_roots.add(u)
                    if save_roots:
                        with open(save_roots, "a") as f:
                            f.write(u + "\n")
                    print(f"\n[discover] found root: {u}")
                    try:
                        total_downloaded += run_one_level(ctx, u, out_dir)
                    except Exception as e:
                        print(f"!! skipped due to error on {u}: {e}")

                    if len(found_roots) >= max_roots:
                        break

            # Keep BFS shallow
            elif p.path.count("/") <= 5 and not PDF_RE.search(u):
                dq.append(u)

        pages_seen += 1
        page.close()

    return total_downloaded, len(found_roots), pages_seen

def try_expand(page):
    # Try to expand course/catalog sections so all "Download" buttons are visible
    for txt in ["Expand", "Show more", "Load more", "Catalog", "All", "Everything",
                "展開", "顯示更多", "載入更多", "目錄", "全部", "所有"]:
        try:
            el = page.get_by_text(txt, exact=False).first
            if el and el.is_visible():
                el.click(); page.wait_for_timeout(600)
        except Exception:
            pass

def grab_pdfs_on_page(page, save_dir):
    """
    On a single page, fetch downloads via two paths:
      1) Click "Download" buttons and capture the download event -> save to folder
      2) Collect all direct .pdf links -> GET and save to folder
    """
    ensure_dir(save_dir)
    downloaded = 0

    # 1) Click likely download buttons
    click_selectors = [
        'a:has-text("下載")', 'button:has-text("下載")',
        'a:has-text("Download")', 'button:has-text("Download")',
        '[aria-label*="下載"]', '[aria-label*="download"]',
    ]
    candidates = page.locator(", ".join(click_selectors))
    count = candidates.count()
    for i in range(count):
        btn = candidates.nth(i)
        try:
            if not btn.is_visible(): continue
            btn.scroll_into_view_if_needed(timeout=1000)
            with page.expect_download(timeout=3000) as dl_info:
                btn.click()
            dl = dl_info.value
            fn = sanitize(dl.suggested_filename)
            dl.save_as(os.path.join(save_dir, fn))
            print(f"    ✓ click-download: {fn}")
            downloaded += 1
        except Exception:
            # If no download event was triggered (e.g., external link or extra step), skip
            pass

    # 2) Fetch all direct .pdf links
    for u in [u for u in abs_links(page) if PDF_RE.search(u)]:
        try:
            resp = page.request.get(u)
            if not resp.ok: continue
            ct = (resp.headers.get("content-type","")).lower()
            if "pdf" not in ct: continue
            # Filename: prefer Content-Disposition over URL
            cd = resp.headers.get("content-disposition","")
            if "filename=" in cd:
                import re
                m = re.search(r'filename\*?=([^;]+)', cd, re.IGNORECASE)
                if m:
                    raw = m.group(1).strip().strip('"').strip("'")
                    fname = sanitize(raw.split("''")[-1])
                else:
                    fname = sanitize(os.path.basename(urlparse(u).path) or "file.pdf")
            else:
                fname = sanitize(os.path.basename(urlparse(u).path) or "file.pdf")
            if not fname.lower().endswith(".pdf"): fname += ".pdf"
            with open(os.path.join(save_dir, fname), "wb") as f:
                f.write(resp.body())
            print(f"    ✓ direct-pdf: {fname}")
            downloaded += 1
        except Exception as e:
            print(f"    ! direct error: {u} ({e})")
    return downloaded

def run_one_level(ctx, url, out_dir):
    page = ctx.new_page(); page.set_default_timeout(20000)
    # Always use the flat output directory (no subfolders)
    save_dir = os.path.abspath(out_dir)
    ensure_dir(save_dir)

    print(f"\n=== Page: {url}")
    resp = safe_goto(page, url, attempts=4)
    if not resp:
        print("  ⚠️ Could not open page after retries, skipping")
        page.close()
        return 0

    scroll(page, 8)
    try_expand(page)
    scroll(page, 4)

    got = grab_pdfs_on_page(page, save_dir)
    print(f"  → Files downloaded on this page: {got}")
    page.close()
    return got


def main():
    ap = argparse.ArgumentParser(description="One-level batch downloader for megacombo pages.")
    ap.add_argument("--login", action="store_true", help="Login first and persist cookies (login only; no other actions).")
    ap.add_argument("--seed", help="Page to open during login (optional; defaults to site homepage).")
    ap.add_argument("--discover", nargs="*", help="Seed pages to auto-discover megacombo roots (streaming: download immediately).")
    ap.add_argument("--roots-file", help="File containing megacombo URLs (one per line).")
    ap.add_argument("--out", default="downloads", help="Output directory.")
    ap.add_argument("--save-roots", help="Path to append discovered roots (optional).")
    ap.add_argument("--max-roots", type=int, default=500, help="Max number of roots to process.")
    ap.add_argument("--headless", action="store_true", help="Headless mode (for download phase).")
    args = ap.parse_args()

    ensure_dir(args.out)
    state_file = "state.json"

    with sync_playwright() as p:
        # ========== A) Login mode: only handles login, then exits ==========
        if args.login:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=".pw-user",
                headless=False,
            )
            page = ctx.new_page()
            page.goto(args.seed or "https://online-academy.fishhuang.com/")
            print("Please complete login in the opened browser. Once the page is accessible, return here and press Enter to continue…")
            input()
            ctx.storage_state(path=state_file)
            print("✅ Saved login state to state.json")
            ctx.close()
            return

        # ========== B) Download mode: requires state.json ==========
        if not os.path.exists(state_file):
            print("❌ Not logged in or state.json not found. Please run:")
            print('   python downloader.py --login --seed "https://online-academy.fishhuang.com/learning/megacombo"')
            return

        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            accept_downloads=True,
            storage_state=state_file
        )

        # Roots source: either --roots-file or --discover (one is required)
        if args.roots_file:
            with open(args.roots_file) as f:
                roots = [line.strip() for line in f if line.strip()]
            if not roots:
                print("⚠️ No megacombo pages found in roots file.")
                ctx.close(); browser.close(); return
            roots = roots[: args.max_roots]
            print(f"✅ Preparing to process {len(roots)} page(s)")
            total = 0
            for r in roots:
                try:
                    total += run_one_level(ctx, r, args.out)
                except Exception as e:
                    print(f"!! skipped due to error on {r}: {e}")
            ctx.close(); browser.close()
            print(f"\n🎉 Done! Total files downloaded: {total}; Output directory: {os.path.abspath(args.out)}")
            return

        elif args.discover:
            print(f"🔎 Streaming discover & download from {len(args.discover)} seed(s)…")
            total, num_roots, pages_seen = stream_discover_and_download(
                ctx,
                seeds=args.discover,
                out_dir=args.out,
                save_roots=args.save_roots,
                max_pages=100,
                max_roots=args.max_roots,
            )
            ctx.close(); browser.close()
            print(f"\n✅ Discovered {num_roots} roots across {pages_seen} page(s)")
            print(f"🎉 Done! Total files downloaded during discovery: {total}; Output directory: {os.path.abspath(args.out)}")
            return

        else:
            print("❌ Please provide --roots-file or --discover (one is required in download mode).")
            ctx.close(); browser.close(); return


if __name__ == "__main__":
    main()
