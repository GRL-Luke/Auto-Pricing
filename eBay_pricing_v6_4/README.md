# Auto Pricer (LAN)

Amazon-first + eBay-compare pricing tool for LAN use. Finds the current Amazon price, pulls clean eBay comps, and suggests a price that undercuts the lower of the two, rounded to **.99** (e.g., $13.49 → **$12.99**).

> Current build: **v6.4 + abs-low-eBay patch**  
> Stack: Python, Flask, Playwright

---

## 1) Quick start

```bash
# In a fresh/empty folder (to avoid stale files)
pip install -r requirements.txt
python -m playwright install
python app.py
# open the URL printed (e.g., http://127.0.0.1:5000)
```

**Windows tips**
- If Playwright prompts for browser install permissions, allow it.
- If you previously ran an older folder, close it and delete any `__pycache__` folders to avoid stale bytecode.

---

## 2) How it works (high level)

1. **Amazon-first**  
   - Accepts UPC / ASIN / Amazon URL.  
   - Loads the product page, detects **ASIN**, **title**, **price**, and **pack qty** (from product detail tables; falls back to title).  
   - If the product price isn’t visible, it will pull **Offer Listings (New)**.

2. **eBay search & clean-up**  
   - Searches by UPC (with and without leading zeros) and by **Amazon title variants** (including common pack keywords).  
   - Keeps **Brand New** only.  
   - Three-stage filtering using Amazon’s title/pack as the anchor:  
     1) **Strict**: pack must match Amazon’s; title similarity ≥ 0.60; price ≥ 60% of Amazon.  
     2) **Relaxed**: pack matches **or is unknown**; title similarity ≥ 0.50; price ≥ 60% of Amazon.  
     3) **Title-only**: similarity ≥ 0.45; price ≥ 60% of Amazon.  
   - **Final safety net**: if all three return zero rows, use the **raw New-only** eBay results (so we never miss a valid low).

3. **Decision**  
   - Compute a reference cluster (IQR/mode window) for display.  
   - **For the final pick**, take the **absolute lowest eBay total** from the curated set (not the cluster) and compare to Amazon:  
     - If **eBay < Amazon** → **$1 under eBay**, snapped to **same-dollar .99**.  
     - Else → **$1 under Amazon**, snapped to **.99**.  
   - The UI shows the suggestion source and a reference link (Amazon or the chosen eBay listing).

---

## 3) UI fields

**Basic**
- **Product Code / ASIN / URL** – UPC (with or without leading zeros), an ASIN, or an Amazon product URL.
- **Title (optional)** – Only needed if you want to steer the eBay title search.
- **Condition** – `New` (recommended) or `All` (for debugging; may admit noisy eBay rows).

**Advanced**
- **IQR Mult** – Used when displaying the “used set” cluster; does **not** control the final pick (we always compare absolute-low eBay vs Amazon). Default `1.5`.
- **Min/Max Price** – Optional hard clamps.
- **Pages / Retries / Attempts** – Controls eBay pagination and resilient scraping.

**Output blocks**
- **Suggested Price** – The final price to list (lower of Amazon/eBay, `$1 off`, rounded to `.99`). Shows a reference link.
- **Amazon Price** – Detected price, title, ASIN, and pack qty (if found). “Offer Listings (New)” is used as a fallback.
- **eBay Results** – Shows the curated “used” set. Expand “Show raw rows” to see everything scraped.

---

## 4) Tuning (optional)

Some thresholds are easy to tweak in `app.py`:

- In `_pack_title_filter(...)`  
  - `sim_strict=0.60`, `sim_relaxed=0.50`, `sim_last=0.45` (title similarity vs Amazon)  
  - `min_ratio=0.60` (drop eBay totals < 60% of Amazon when pack is comparable)

- Final decision is handled in `pricing.py` → `choose_and_suggest(...)` and `round_same_dollar_to_99(...)`.

If you want these exposed in the UI, we can add advanced inputs.

---

## 5) Troubleshooting

- **“Internal Server Error” on POST**  
  - Most common cause is mixing files from older builds. Use a **fresh folder** and drop in a complete build.  
  - Delete `__pycache__/` and restart (`python app.py`).

- **No eBay rows**  
  - Check that condition is `New`.  
  - Expand “raw rows” to confirm scraping worked; if raw is non-zero but “used” is 0, filtering was too strict. Try again with a small title supplied or relax thresholds.

- **Amazon price missing**  
  - The app will try “Offer Listings (New)”. If still blank, the product may be gated/hidden; try supplying the ASIN directly.

- **Firewall or Playwright issues**  
  - Ensure local loopback is allowed and Playwright browsers installed: `python -m playwright install`.

---

## 6) Known limitations

- Some Amazon pages hide price or pack qty behind variations; we do a best-effort parse (detail tables → title fallback).
- eBay sellers sometimes omit pack quantities. The filter allows “unknown pack” at relaxed stage to avoid losing valid comps.
- Title similarity uses token overlap (Jaccard). Obscure/short titles may under-score; adding a helpful title in the UI improves matches.

---

## 7) What’s included

- `app.py` — UI + orchestration + filtering + final decision (always compares absolute-low eBay vs Amazon).  
- `scraping.py` — Amazon & eBay fetching (UPC normalization, ASIN extraction, title/pack detection, Offer Listings fallback).  
- `pricing.py` — Stats helpers, `.99` rounding, and “undercut lower of Amazon/eBay” logic.  
- `templates/index.html` — The web UI.

---

## 8) Updating

When you receive a new full build:
1) Extract into a **new folder** (do not mix with old files).  
2) Re-run `pip install -r requirements.txt` and `python -m playwright install`.  
3) Start `python app.py`.

For small patches (just `app.py` or `pricing.py`), overwrite the file, delete `__pycache__/`, and restart.

---

## 9) Support

If you find a product where the suggestion doesn’t undercut the lower of Amazon/eBay, send:
- The UPC/ASIN or URL
- A screenshot of the page (including the eBay table header showing “raw” vs “used” counts)
- Which row you believe should be the reference

I’ll tune the thresholds or add a specific guard where needed.
