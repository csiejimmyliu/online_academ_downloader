# Downloader — README

> **Important:** Downloading requires an authenticated session. **You must log in first** (creates `state.json`) before running downloads.

## Setup

```bash
bash ./setup.sh
source venv/bin/activate
```


## Login

```bash
python downloader.py --login --seed "https://online-academy.fishhuang.com/learning/megacombo"
# A browser opens. Log in, then press Enter in the terminal.
```

## Download

### A) Auto-discover pages and download

```bash
python downloader.py --discover "https://online-academy.fishhuang.com/learning/megacombo" --out downloads
```

### B) From a list (one URL per line in roots.txt)

```bash
python downloader.py --roots-file roots.txt --out downloads
```

## Notes

* Login state is saved to `state.json`.
* All files are saved into `downloads/` (no subfolders).
* Add `--headless` to run without a visible browser.
