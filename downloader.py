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

def discover_megacombos(ctx, seeds, max_pages=80):
    """Breadth-first discovery of all megacombo root pages (single level)."""
    found = set(); visited = set(); dq = collections.deque(seeds)
    while dq and len(visited) < max_pages:
        url = dq.popleft()
        if url in visited: continue
        visited.add(url)
        page = ctx.new_page(); page.set_default_timeout(15000)
        try:
            page.goto(url)
        except PWTimeoutError:
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
    try:
        page.goto(url)
    except PWTimeoutError:
        print("  ⚠️ Load timeout, skipping"); page.close(); return 0

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
    ap.add_argument("--discover", nargs="*", help="Seed pages to auto-discover megacombo roots (one or more).")
    ap.add_argument("--roots-file", help="File containing megacombo URLs (one per line).")
    ap.add_argument("--out", default="downloads", help="Output directory.")
    ap.add_argument("--save-roots", help="Write discovered roots to this file.")
    ap.add_argument("--max-roots", type=int, default=500, help="Max number of roots to process.")
    ap.add_argument("--headless", action="store_true", help="Headless mode (for download phase).")
    args = ap.parse_args()

    ensure_dir(args.out)
    state_file = "state.json"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        # ========== A) Login mode: only handles login, then exits ==========
        if args.login:
            # Use a persistent context so login data is written to disk
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=".pw-user",
                headless=False,
            )
            page = ctx.new_page()
            page.goto(args.seed or "https://online-academy.fishhuang.com/")
            print("Please complete login in the opened browser. Once the page is accessible, return here and press Enter to continue…")
            input()
            # Save login state
            ctx.storage_state(path=state_file)
            print("✅ Saved login state to state.json")
            # Close and exit (no discover/download in login mode)
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
        roots = []
        if args.roots_file:
            with open(args.roots_file) as f:
                roots = [line.strip() for line in f if line.strip()]
        elif args.discover:
            print(f"🔎 Discovering megacombo pages from {len(args.discover)} seed(s)…")
            roots = discover_megacombos(ctx, args.discover, max_pages=100)
            if args.save_roots:
                with open(args.save_roots, "w") as f:
                    f.write("\n".join(roots) + "\n")
                print(f"📝 Roots written to: {os.path.abspath(args.save_roots)}")
        else:
            print("❌ Please provide --roots-file or --discover (one is required in download mode).")
            ctx.close(); browser.close(); return

        if not roots:
            print("⚠️ No megacombo pages found.")
            ctx.close(); browser.close(); return

        roots = roots[: args.max_roots]
        print(f"✅ Preparing to process {len(roots)} page(s)")

        total = 0
        for r in roots:
            total += run_one_level(ctx, r, args.out)

        ctx.close(); browser.close()
        print(f"\n🎉 Done! Total files downloaded: {total}; Output directory: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
