
from __future__ import annotations
import os, asyncio, re
from flask import Flask, render_template, request
from scraping import scrape_multi, detect_pack_qty
from pricing import compute_suggestion, choose_and_suggest, tokens, jaccard

app = Flask(__name__)

# ------------------------------ Matching helpers ------------------------------

HEAD_NOUNS = {
    "toothpaste","mouthwash","toothbrush","filter","filters","cartridge","cartridges",
    "shampoo","conditioner","lotion","soap","detergent","pods","protein","powder",
    "kibble","treats","biscuits","supplement","supplements","cleaner","deodorant",
    "razor","blades","sponges","bags","diapers","wipes","vitamin","vitamins",
    "capsules","tablets","syrup","sauce","cereal","snack","bar","bars","socks",
    "battery","batteries","charger","cable","hose","spray","gel","cream"
}

def _norm(s: str) -> str:
    return re.sub(r"[\\s\\-\\._/()]+", "", (s or "").lower())

def _filter_required(rows, required_csv: str):
    req = [r.strip() for r in (required_csv or "").split(",") if r.strip()]
    if not req:
        return rows
    norm_reqs = [_norm(x) for x in req]
    kept = []
    for r in rows:
        title_norm = _norm(r.get("title",""))
        if all(n in title_norm for n in norm_reqs):
            kept.append(r)
    return kept

def extract_brand_from_title(title: str) -> str | None:
    if not title:
        return None
    words = re.findall(r"[A-Za-z][A-Za-z0-9'\\-]+", title)
    if not words:
        return None
    return words[0].lower()

def extract_sizes(text: str):
    """Return list of (value(float), unit(str normalized)) found in text."""
    if not text:
        return []
    t = text.lower().replace("fluid ounce","fl oz").replace("ounces","oz").replace("ounce","oz")
    t = t.replace("pounds","lb").replace("pound","lb").replace("kilogram","kg").replace("grams","g")
    sizes = []
    for m in re.finditer(r"([0-9]+(?:\\.[0-9]+)?)\\s*(fl\\s*oz|floz|oz|lb|ml|g|kg)", t):
        val = float(m.group(1))
        unit = m.group(2).replace(" ","")
        if unit == "floz": unit = "floz"
        sizes.append((val, unit))
    return sizes

def size_match(amz_sizes, row_sizes, tol=0.12):
    if not amz_sizes or not row_sizes:
        return False
    for av, au in amz_sizes:
        for rv, ru in row_sizes:
            if au == ru and abs(rv - av) <= max(0.05, av * tol):
                return True
    return False

def head_noun_match(amz_title: str, row_title: str) -> bool:
    amz_words = set(re.findall(r"[a-z]+", amz_title.lower()))
    nouns = [w for w in HEAD_NOUNS if w in amz_words]
    if not nouns:
        return False
    row_low = row_title.lower()
    return any(n in row_low for n in nouns)

def brand_present(brand: str | None, row_title: str) -> bool:
    if not brand:
        return True
    return brand.lower() in row_title.lower()

# ------------------------------ App scaffolding ------------------------------

def default_ctx():
    return {
        "form": {
            "code": "",
            "title": "",
            "req": "",
            "condition": "new",
            "iqr_mult": "1.5",
            "min_price": "",
            "max_price": "",
            "pages": "1",
            "retries": "2",
            "attempts": "4",
        },
        "error": None,
        "amazon": None,
        "amazon_note": None,
        "results": None,
        "results_raw": None,
        "suggestion": None,
        "suggestion_source": None,
        "reference": None,
        "secondary": None,
        "secondary_ref": None,
        "counts": None,
    }

def _pack_title_filter(rows, amazon, sim_strict=0.60, sim_relaxed=0.50, sim_last=0.45, min_ratio=0.60):
    """
    Strict -> relaxed -> title-only filter with brand+anchors. If this returns
    an empty list, the caller will now do a soft fallback (partial brand + lower
    similarity), so we always produce a suggestion when data exists.
    """
    if not rows or not amazon:
        return rows

    amz_total = amazon.get("total")
    amz_title = amazon.get("title") or ""
    amz_toks = tokens(amz_title)
    amz_pack = amazon.get("pack_qty")
    amz_brand = extract_brand_from_title(amz_title)
    amz_sizes = extract_sizes(amz_title)

    def anchors_ok(row_title: str) -> bool:
        ok = 0
        if head_noun_match(amz_title, row_title):
            ok += 1
        rp = detect_pack_qty(row_title or "")
        if amz_pack and rp == amz_pack:
            ok += 1
        if size_match(amz_sizes, extract_sizes(row_title)):
            ok += 1
        return ok >= 2

    def ok_floor(r):
        if amz_total is None or r.get("total") is None:
            return True
        t = float(r["total"])
        if t < float(amz_total) * 0.60:
            if not (brand_present(amz_brand, r.get("title","")) and anchors_ok(r.get("title",""))):
                return False
        return True

    def pack_rule(row_title: str, allow_unknown: bool):
        rp = detect_pack_qty(row_title or "")
        if amz_pack is None:
            return True
        if allow_unknown and rp is None:
            return True
        return rp == amz_pack

    def sim_ok(r, thr):
        if not amz_toks:
            return True
        s = jaccard(amz_toks, tokens(r.get("title") or ""))
        if amz_total is not None and r.get("total") is not None:
            if float(r["total"]) < float(amz_total) * 0.60:
                return s >= 0.60
        return s >= thr

    def brand_ok(r):
        return brand_present(amz_brand, r.get("title",""))

    strict = [r for r in rows if brand_ok(r) and ok_floor(r) and sim_ok(r, sim_strict)
              and pack_rule(r.get("title",""), allow_unknown=False)
              and anchors_ok(r.get("title",""))]
    if strict:
        return strict

    relaxed = [r for r in rows if brand_ok(r) and ok_floor(r) and sim_ok(r, sim_relaxed)
               and pack_rule(r.get("title",""), allow_unknown=True)
               and anchors_ok(r.get("title",""))]
    if relaxed:
        return relaxed

    last = [r for r in rows if brand_ok(r) and ok_floor(r) and sim_ok(r, sim_last)]
    return last

def _soft_fallback(rows, amazon):
    """
    Soft fallback used only if strict/relaxed/title filters return 0.
    This loosens brand to partial match and similarity to 0.40 and the
    price floor to 50% of Amazon.
    """
    if not rows:
        return rows
    amz_title = (amazon.get("title") if amazon else "") or ""
    amz_toks = tokens(amz_title)
    amz_total = amazon.get("total") if amazon else None
    amz_brand = extract_brand_from_title(amz_title)
    amz_pack = amazon.get("pack_qty")
    amz_sizes = extract_sizes(amz_title)

    def ok_floor(r):
        if amz_total is None or r.get("total") is None:
            return True
        return float(r["total"]) >= float(amz_total) * 0.50

    def sim_ok(r):
        if not amz_toks:
            return True
        return jaccard(amz_toks, tokens(r.get("title") or "")) >= 0.40

    def pack_or_size_ok(title: str):
        rp = detect_pack_qty(title or "")
        if amz_pack and rp == amz_pack:
            return True
        if size_match(amz_sizes, extract_sizes(title)):
            return True
        return False

    kept = []
    for r in rows:
        title = r.get("title","")
        if amz_brand and (amz_brand in title.lower()) or (not amz_brand):
            if ok_floor(r) and sim_ok(r):
                # prefer items that also agree on pack/size, but don't require it
                kept.append(r)
    # If even that yields nothing, just return rows; caller still picks absolute-low
    return kept or rows

# ------------------------------ Routes ------------------------------

@app.route("/clear", methods=["GET"])
def clear():
    return render_template("index.html", **default_ctx())

@app.route("/", methods=["GET", "POST"])
def index():
    ctx = default_ctx()
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        title = (request.form.get("title") or "").strip() or None
        req_tokens = (request.form.get("req") or "").strip()
        condition = (request.form.get("condition") or "new").strip().lower()
        iqr_mult = float(request.form.get("iqr_mult") or 1.5)
        min_price = float(request.form.get("min_price")) if request.form.get("min_price") else None
        max_price = float(request.form.get("max_price")) if request.form.get("max_price") else None
        pages = max(1, int(request.form.get("pages") or 1))
        retries = max(1, int(request.form.get("retries") or 2))
        attempts = max(1, int(request.form.get("attempts") or 4))

        ctx["form"].update({
            "code": code or "",
            "title": title or "",
            "req": req_tokens,
            "condition": condition,
            "iqr_mult": str(iqr_mult),
            "min_price": "" if min_price is None else str(min_price),
            "max_price": "" if max_price is None else str(max_price),
            "pages": str(pages),
            "retries": str(retries),
            "attempts": str(attempts),
        })

        if not code:
            ctx["error"] = "Enter a product code, ASIN, or Amazon URL."
            return render_template("index.html", **ctx)

        try:
            data = asyncio.run(scrape_multi(
                code, title,
                condition=condition,
                prefer_amazon_first=True,
                use_amazon=True,
                pages=pages,
                retries=retries,
                attempts=attempts,
                visible=False,
            ))
        except Exception as e:
            ctx["error"] = f"Search failed: {e}"
            return render_template("index.html", **ctx)

        raw_rows = data.get("rows", [])
        amazon = data.get("amazon")
        rows = _filter_required(raw_rows, req_tokens) if raw_rows else []

        comp_rows = _pack_title_filter(rows, amazon)

        # NEW: soft fallback if strict pipeline produced nothing
        if not comp_rows:
            comp_rows = _soft_fallback(rows, amazon)

        # Compute cluster (display only)
        ebay_low, ebay_ref_row, ebay_used_rows = (None, None, [])
        if comp_rows:
            ebay_low, ebay_ref_row, ebay_used_rows = compute_suggestion(
                comp_rows,
                iqr_mult=iqr_mult,
                min_price=min_price,
                max_price=max_price,
                method="mode",
                window=10.0
            )

        # Absolute-low eBay from chosen set
        ebay_abs_row = None
        ebay_abs_total = None
        if comp_rows:
            valid = [r for r in comp_rows if r.get("total") is not None]
            if valid:
                ebay_abs_row = min(valid, key=lambda r: r["total"])
                ebay_abs_total = float(ebay_abs_row["total"])

        amz_total = float(amazon["total"]) if (amazon and amazon.get("total") is not None) else None

        pick = choose_and_suggest(amz_total, ebay_abs_total)

        ctx["amazon"] = amazon
        if amazon and amz_total is not None:
            pack_msg = f" (pack qty detected: {amazon.get('pack_qty')})" if amazon and amazon.get("pack_qty") else ""
            ctx["amazon_note"] = "Amazon price from product page; if missing, pulled from Offer Listings (New)" + pack_msg + "."

        ctx["results"] = comp_rows
        ctx["results_raw"] = raw_rows
        ctx["counts"] = {"raw": len(rows), "used": len(comp_rows)}

        if pick["suggested"] is not None:
            ctx["suggestion"] = f"{pick['suggested']:.2f}"
            ctx["suggestion_source"] = pick["source"]
            if pick["source"] == "Amazon":
                ctx["reference"] = amazon.get("url") if amazon else None
            else:
                ctx["reference"] = ebay_abs_row.get("url") if ebay_abs_row else (ebay_ref_row.get("url") if ebay_ref_row else None)
        else:
            ctx["error"] = "Not enough clean data to suggest a price."

        return render_template("index.html", **ctx)

    return render_template("index.html", **ctx)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")), debug=False)
