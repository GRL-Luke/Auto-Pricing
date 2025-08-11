
from __future__ import annotations
import asyncio, re, urllib.parse
from typing import Optional, Dict, List, Tuple, Set
from urllib.parse import quote_plus
from playwright.async_api import async_playwright

PRICE_RE = re.compile(r"\$?\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)")
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})", re.I)
EBAY_ITM_RE = re.compile(r"/itm/(\d{11,14})")

STOP = set("for with the and of to by from in on a an new pack filters filter water large small medium size sizes 2 3 4 5 6 7 8 box".split())

def normalize_upc(s: str) -> str:
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    return digits.lstrip("0")

def tokens(s: str) -> Set[str]:
    if not s:
        return set()
    t = re.sub(r"[^A-Za-z0-9]+"," ", s).lower().split()
    return {w for w in t if len(w) > 2 and w not in STOP}

def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b: return 0.0
    return len(a & b) / float(len(a | b))

WORD_NUM = {
    "single": 1, "one": 1, "two": 2, "twin": 2, "double": 2, "duo": 2,
    "three": 3, "triple": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10
}

def parse_money(text: str) -> Optional[float]:
    if not text:
        return None
    m = PRICE_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def extract_asin_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = ASIN_RE.search(url)
    return m.group(1).upper() if m else None

def detect_pack_qty(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.lower()
    for pat in [
        r"pack of\s*(\d+)",
        r"(\d+)\s*-\s*pack\b",
        r"(\d+)\s*pack\b",
        r"(\d+)\s*pk\b",
        r"(\d+)\s*count\b",
        r"(\d+)\s*ct\b",
        r"(\d+)\s*pcs\b",
        r"(\d+)\s*pieces\b",
        r"(\d+)\s*capsules\b",
    ]:
        m = re.search(pat, t)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    for w, n in WORD_NUM.items():
        if re.search(rf"\b{re.escape(w)}[-\s]?pack\b", t):
            return n
    m = re.search(r"\b(\d+)\s*(?:pk|ct)\b", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    m = re.search(r"\bx\s*(\d+)\b", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None

async def _dismiss(page):
    for name in ["Accept", "I agree", "Got it", "Accept all", "OK"]:
        try:
            btn = page.get_by_role("button", name=name)
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=800)
        except Exception:
            pass

async def _extract_until(page, selectors: List[str], total_ms: int = 8000) -> Optional[float]:
    step = 400
    waited = 0
    while waited <= total_ms:
        for sel in selectors:
            try:
                el = page.locator(sel)
                if await el.first.is_visible(timeout=300):
                    txt = (await el.first.inner_text()) or ""
                    val = parse_money(txt)
                    if val is not None:
                        return val
            except Exception:
                continue
        await page.wait_for_timeout(step)
        waited += step
    return None

# ---------- Amazon ----------
async def _extract_amazon_price_from_product(page) -> Optional[float]:
    selectors = [
        "#corePrice_feature_div span.a-offscreen",
        "#apex_desktop span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "#tp_price_block_total_price_ww",
        "#newBuyBoxPrice",
        "[data-a-color='price'] .a-offscreen",
        "span.a-price .a-offscreen",
    ]
    price = await _extract_until(page, selectors, total_ms=9000)
    if price is not None:
        return price
    try:
        txt = await page.locator("div#desktop_qualifiedBuyBox").inner_text()
        price = parse_money(txt)
        if price is not None:
            return price
    except Exception:
        pass
    try:
        offers = page.locator("a#buybox-see-all-buying-choices-announce, a:has-text('See All Buying Options')")
        if await offers.first.is_visible(timeout=800):
            await offers.first.click()
            await page.wait_for_load_state("networkidle")
            price = await _extract_until(page, ["span.a-price .a-offscreen"], total_ms=6000)
            if price is not None:
                return price
    except Exception:
        pass
    return None

async def _extract_from_offer_list_page(page) -> Optional[float]:
    selectors = [
        "div.olpOfferPrice, span.olpOfferPrice",
        "span.a-price .a-offscreen",
        "span.a-price-whole"
    ]
    price = await _extract_until(page, selectors, total_ms=6000)
    return price

async def fetch_amazon_from_asin(play, asin: str, timeout_ms: int = 45000) -> Optional[Dict]:
    asin = (asin or "").strip().upper()
    if not asin or not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return None
    browser = await play.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
    )
    page = await context.new_page()
    try:
        url = f"https://www.amazon.com/dp/{asin}?psc=1"
        await page.goto(url, timeout=timeout_ms)
        await _dismiss(page)
        title = ""
        try:
            t = page.locator("#productTitle")
            if await t.is_visible(timeout=2500):
                title = (await t.inner_text()).strip()
        except Exception:
            pass
        if not title:
            try:
                title = (await page.title()) or ""
            except Exception:
                pass
        price = await _extract_amazon_price_from_product(page)
        if price is None:
            offers_url = f"https://www.amazon.com/gp/offer-listing/{asin}?f_new=true"
            await page.goto(offers_url, timeout=timeout_ms)
            await _dismiss(page)
            price = await _extract_from_offer_list_page(page)
        if price is None:
            mob_offers = f"https://www.amazon.com/gp/aw/ol/{asin}?condition=new"
            await page.goto(mob_offers, timeout=timeout_ms)
            await _dismiss(page)
            price = await _extract_from_offer_list_page(page)
        pack_qty = await _infer_pack_qty_from_page(page)
        if not pack_qty:
            pack_qty = detect_pack_qty(title)
        return {"source": "Amazon", "title": title, "price": price, "shipping": 0.0, "total": price, "url": url, "asin": asin, "pack_qty": pack_qty}
    finally:
        await context.close()
        await browser.close()

async def _amazon_search_cards(page) -> List[Dict]:
    await page.wait_for_selector("div.s-main-slot div[data-component-type='s-search-result']", timeout=45000)
    cards = await page.query_selector_all("div.s-main-slot div[data-component-type='s-search-result']")
    out = []
    for c in cards:
        try:
            sp = await c.query_selector("span.s-label-popover-default, span.puis-sponsored-label-text")
            if sp and "sponsored" in (await sp.inner_text()).strip().lower():
                continue
            a = await c.query_selector("h2 a")
            href = await a.get_attribute("href") if a else None
            title = (await a.inner_text()).strip() if a else ""
            data_asin = (await c.get_attribute("data-asin")) or ""
            asin = data_asin.strip().upper() if data_asin else None
            price_span = await c.query_selector("span.a-price > span.a-offscreen")
            price_text = (await price_span.inner_text()).strip() if price_span else ""
            card_price = parse_money(price_text)
            full_url = f"https://www.amazon.com/dp/{asin}?psc=1" if asin else ("https://www.amazon.com"+href if href and href.startswith("/") else href)
            out.append({"title": title, "url": full_url, "asin": asin, "card_price": card_price})
        except Exception:
            continue
    return out

async def fetch_amazon_by_search(play, query: str, timeout_ms: int = 60000, per_item_timeout_ms: int = 35000, max_candidates: int = 8) -> Optional[Dict]:
    browser = await play.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
    )
    page = await context.new_page()
    try:
        url = f"https://www.amazon.com/s?k={quote_plus(query)}"
        await page.goto(url, timeout=timeout_ms)
        await _dismiss(page)
        cards = await _amazon_search_cards(page)
        if not cards:
            return None
        cards = sorted(cards, key=lambda c: (0 if c.get("asin") else 1))
        for cand in cards[:max_candidates]:
            prod = await context.new_page()
            try:
                target_url = cand["url"]
                await prod.goto(target_url, timeout=per_item_timeout_ms)
                await _dismiss(prod)
                title = ""
                try:
                    t = prod.locator("#productTitle")
                    if await t.is_visible(timeout=1500):
                        title = (await t.inner_text()).strip()
                except Exception:
                    pass
                price = await _extract_amazon_price_from_product(prod)
                pack_qty = await _infer_pack_qty_from_page(prod)
                if not pack_qty:
                    pack_qty = detect_pack_qty(title or cand.get("title",""))
                if price is None and cand.get("asin"):
                    offers_url = f"https://www.amazon.com/gp/offer-listing/{cand['asin']}?f_new=true"
                    await prod.goto(offers_url, timeout=per_item_timeout_ms)
                    await _dismiss(prod)
                    price = await _extract_from_offer_list_page(prod)
                if price is None:
                    price = cand.get("card_price")
                if price is None:
                    continue
                return {
                    "source": "Amazon",
                    "title": title or cand.get("title",""),
                    "price": price, "shipping": 0.0, "total": price,
                    "url": target_url, "asin": cand.get("asin"), "pack_qty": pack_qty
                }
            except Exception:
                continue
            finally:
                try: await prod.close()
                except Exception: pass
        return None
    finally:
        await context.close()
        await browser.close()

# ---------- eBay ----------

def _canon_itm(url: str) -> str:
    if not url:
        return ""
    m = EBAY_ITM_RE.search(url)
    if not m:
        return ""
    itemid = m.group(1)
    return f"https://www.ebay.com/itm/{itemid}"

def _unwrap_ebay_url(href: str) -> Optional[str]:
    if not href:
        return None
    try:
        if "/p/" in href:
            return None
        canon = _canon_itm(href)
        if canon:
            return canon
        parsed = urllib.parse.urlparse(href)
        q = urllib.parse.parse_qs(parsed.query)
        for vals in q.values():
            for v in vals:
                u = urllib.parse.unquote(v)
                canon = _canon_itm(u)
                if canon:
                    return canon
        u = urllib.parse.unquote(href)
        canon = _canon_itm(u)
        if canon:
            return canon
    except Exception:
        pass
    return None

def _is_brand_new_only(cond_text: str) -> bool:
    if not cond_text:
        return False
    t = cond_text.strip().lower()
    bad = [
        "new (other", "new - other", "new—other", "new – other", "new: other",
        "open box", "open-box", "opened", "refurb", "seller refurbished",
        "manufacturer refurbished", "used", "pre-owned", "like new", "without box", "damaged box"
    ]
    if any(b in t for b in bad):
        return False
    return t == "new" or t.startswith("brand new")

async def _auto_scroll(page, steps: int = 8, delay_ms: int = 250):
    h = await page.evaluate("() => document.body.scrollHeight")
    step = max(300, int(h / steps))
    pos = 0
    for _ in range(steps):
        pos += step
        await page.evaluate(f"window.scrollTo(0, {pos});")
        await page.wait_for_timeout(delay_ms)

async def fetch_ebay_query(play, query: str, condition: str = "new", timeout_ms: int = 22000, visible: bool = False, pages: int = 1, retries: int = 2) -> List[Dict]:
    browser = await play.chromium.launch(headless=(not visible))
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    )
    page = await context.new_page()
    results: List[Dict] = []
    try:
        base = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&rt=nc&LH_BIN=1"
        if condition.lower() == "new":
            base += "&LH_ItemCondition=1000"
        for p in range(1, max(1, pages)+1):
            url = base + (f"&_pgn={p}" if p > 1 else "")
            found_page_items = False
            for attempt in range(retries):
                await page.goto(url, timeout=timeout_ms)
                await page.wait_for_selector("li.s-item, div.s-item, div.s-item__wrapper, ul.srp-results", timeout=timeout_ms)
                await _auto_scroll(page)
                items = await page.query_selector_all("li.s-item, div.s-item, div.s-item__wrapper")
                if items:
                    found_page_items = True
                    break
            if not found_page_items:
                continue
            for it in items:
                title_el = await it.query_selector("a.s-item__link, a.s-item__title, h3.s-item__title a, a[href*='/itm/']")
                if not title_el:
                    continue
                raw_url = await title_el.get_attribute("href")
                url = _unwrap_ebay_url(raw_url)
                if not url:
                    continue
                title = (await title_el.inner_text()) if title_el else ""
                if title and title.strip().lower().startswith("shop on ebay"):
                    continue
                badge = await it.query_selector("span.s-item__ad-badge-text")
                if badge:
                    try:
                        badge_text = (await badge.inner_text()).strip().lower()
                        if "sponsored" in badge_text:
                            continue
                    except Exception:
                        pass
                price_el = await it.query_selector("span.s-item__price")
                price_text = (await price_el.inner_text()) if price_el else ""
                if " to " in (price_text or "").lower():
                    continue
                price = parse_money(price_text)
                ship_el = await it.query_selector("span.s-item__shipping, span.s-item__logisticsCost")
                ship_text = (await ship_el.inner_text()) if ship_el else ""
                if ship_text and "free" in ship_text.lower():
                    shipping = 0.0
                else:
                    shipping = parse_money(ship_text)
                total = None
                if price is not None:
                    total = price + (shipping if shipping is not None else 0.0)
                cond_el = await it.query_selector("span.SECONDARY_INFO")
                cond_text = (await cond_el.inner_text()) if cond_el else ""
                if condition.lower() == "new" and not _is_brand_new_only(cond_text or ""):
                    continue
                if total is not None:
                    results.append({
                        "source": "eBay",
                        "query": query,
                        "title": title.strip(),
                        "price": price,
                        "shipping": shipping,
                        "total": total,
                        "condition": cond_text.strip() if cond_text else "",
                        "url": url,
                        "has_code": False
                    })
        return results
    finally:
        await context.close()
        await browser.close()

async def scrape_multi(
    code: str,
    title: Optional[str],
    condition: str = "new",
    prefer_amazon_first: bool = True,
    use_amazon: bool = True,
    pages: int = 1,
    retries: int = 2,
    attempts: int = 4,
    visible: bool = False,
) -> Dict:
    normalized_code = normalize_upc(code)
    async with async_playwright() as play:
        amazon_result = None
        if use_amazon:
            asin_direct = None
            if code and code.startswith("http"):
                asin_direct = extract_asin_from_url(code)
            elif code and re.fullmatch(r"[A-Za-z0-9]{10}", code or ""):
                asin_direct = code.upper()
            if asin_direct:
                try:
                    amazon_result = await fetch_amazon_from_asin(play, asin_direct)
                except Exception:
                    amazon_result = None
            if not amazon_result and code:
                try:
                    amazon_result = await fetch_amazon_by_search(play, code)
                except Exception:
                    amazon_result = None
            if not amazon_result and title:
                try:
                    amazon_result = await fetch_amazon_by_search(play, title)
                except Exception:
                    amazon_result = None

        expected_pack_qty = amazon_result.get("pack_qty") if amazon_result else None

        rows: List[Dict] = []

        # UPC-first
        queries = []
        if code:
            queries.append(code)
            stripped = normalize_upc(code)
            if stripped and stripped != code:
                queries.append(stripped)

        # if UPC queries fail, fall back to Amazon-title variants
        amz_title = (amazon_result or {}).get("title") or title or ""
        base_toks = tokens(amz_title)
        if amz_title:
            variants = [amz_title]
            if expected_pack_qty and expected_pack_qty > 1:
                variants += [f"{amz_title} {expected_pack_qty} pack",
                             f"{amz_title} {expected_pack_qty}-pack",
                             f"{amz_title} {expected_pack_qty}pk",
                             f"pack of {expected_pack_qty} {amz_title}"]
            queries += variants

        # run queries until we get some candidate rows
        for q in queries:
            if len(rows) >= 3:  # sufficient pool
                break
            try:
                batch = await fetch_ebay_query(play, q, condition=condition, visible=False, pages=pages, retries=retries)
                rows.extend(batch)
            except Exception:
                continue

        # de-dup
        dedup = {r["url"]: r for r in rows}
        rows = list(dedup.values())

        # apply pack qty filter and title similarity if we have an Amazon title
        filtered: List[Dict] = []
        for r in rows:
            # pack filter
            if expected_pack_qty:
                q = detect_pack_qty(r.get("title") or "")
                if q is not None and q != expected_pack_qty:
                    continue
                if q is None and expected_pack_qty > 1:
                    continue
            # title similarity
            if base_toks:
                sim = jaccard(base_toks, tokens(r.get("title") or ""))
                if sim < 0.45:
                    continue
            filtered.append(r)

        return {"rows": filtered or rows, "amazon": amazon_result, "meta": {"count": len(filtered or rows), "expected_pack_qty": expected_pack_qty, "normalized_code": normalized_code}}

async def _infer_pack_qty_from_page(page) -> Optional[int]:
    selectors = [
        "table#productDetails_techSpec_section_1",
        "table#productDetails_detailBullets_sections1",
        "table.prodDetTable",
        "#detailBullets_feature_div"
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                txt = (await loc.inner_text()) or ""
                for key in ["Item Package Quantity", "Unit Count", "Count", "Pack"]:
                    m = re.search(rf"{key}[^0-9]*([0-9]+)", txt, re.I)
                    if m:
                        return int(m.group(1))
                q = detect_pack_qty(txt)
                if q:
                    return q
        except Exception:
            continue
    return None
