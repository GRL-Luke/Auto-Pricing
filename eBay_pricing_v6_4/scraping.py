
from __future__ import annotations
import asyncio, re, urllib.parse, urllib.request
from contextlib import asynccontextmanager
from html import unescape
from typing import Optional, Dict, List, Set
from urllib.parse import quote_plus

try:  # Optional; fall back to stdlib HTTP client if unavailable
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    httpx = None  # type: ignore

try:  # Optional parser (BeautifulSoup)
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    BeautifulSoup = None  # type: ignore

from playwright.async_api import async_playwright

PRICE_RE = re.compile(r"\$?\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)")
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})", re.I)
EBAY_ITM_RE = re.compile(r"/itm/(\d{11,14})")

STOP = set("for with the and of to by from in on a an new pack filters filter water large small medium size sizes 2 3 4 5 6 7 8 box".split())

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

AMAZON_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.amazon.com/",
}

EBAY_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.ebay.com/",
}

if httpx is not None:
    HTTP_TIMEOUT = httpx.Timeout(20.0, connect=20.0)  # type: ignore[attr-defined]
else:
    HTTP_TIMEOUT = 20.0

TAG_RE = re.compile(r"<[^>]+>")

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


@asynccontextmanager
async def optional_playwright():
    try:
        async with async_playwright() as play:
            yield play
    except Exception:
        yield None

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

async def _http_get(url: str, headers: Dict[str, str]) -> Optional[str]:
    if httpx is not None:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:  # type: ignore[arg-type]
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception:
            return None

    def _sync_fetch() -> Optional[str]:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:  # type: ignore[arg-type]
                data = resp.read()
            return data.decode("utf-8", errors="ignore")
        except Exception:
            return None

    return await asyncio.to_thread(_sync_fetch)

def _first_text(soup, selectors: List[str]) -> str:
    if soup is None:
        return ""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    return ""

def _first_price(soup, selectors: List[str]) -> Optional[float]:
    if soup is None:
        return None
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            price = parse_money(el.get_text(" ", strip=True))
            if price is not None:
                return price
    return None

def _make_soup(html: str):
    if BeautifulSoup is None:
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        return None

def _strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = TAG_RE.sub(" ", text)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

def _extract_via_regex(patterns: List[str], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            return m.group(1)
    return ""

def _parse_amazon_product_from_html(html: str, url: str, asin: Optional[str] = None) -> Optional[Dict]:
    soup = _make_soup(html)
    title = _first_text(soup, ["#productTitle", "span#productTitle", "h1"])
    if not title and soup is not None and getattr(soup, "title", None):
        try:
            title = soup.title.get_text(" ", strip=True)
        except Exception:
            title = ""
    if not title:
        title = _strip_html(_extract_via_regex([
            r'id\s*=\s*"productTitle"[^>]*>(.*?)<',
            r'<title>(.*?)</title>',
        ], html))
    price = _first_price(soup, [
        "#corePrice_feature_div span.a-offscreen",
        "#apex_desktop span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "#tp_price_block_total_price_ww",
        "#newBuyBoxPrice",
        "span.a-price .a-offscreen",
        "span.a-price-whole",
    ])
    if price is None:
        raw_price = _extract_via_regex([
            r'class\s*=\s*"a-offscreen"[^>]*>([^<]+)<',
            r'class\s*=\s*"a-price-whole"[^>]*>([^<]+)<',
        ], html)
        price = parse_money(raw_price)
    if price is None:
        return None
    pack_qty = detect_pack_qty(title)
    return {
        "source": "Amazon",
        "title": title,
        "price": price,
        "shipping": 0.0,
        "total": price,
        "url": url,
        "asin": asin,
        "pack_qty": pack_qty,
    }

async def fetch_amazon_http_by_asin(asin: str) -> Optional[Dict]:
    asin = (asin or "").strip().upper()
    if not asin or not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return None
    url = f"https://www.amazon.com/dp/{asin}?psc=1"
    html = await _http_get(url, AMAZON_HEADERS)
    if not html:
        return None
    return _parse_amazon_product_from_html(html, url, asin)

async def fetch_amazon_http_by_search(query: str) -> Optional[Dict]:
    query = (query or "").strip()
    if not query:
        return None
    url = f"https://www.amazon.com/s?k={quote_plus(query)}"
    html = await _http_get(url, AMAZON_HEADERS)
    if not html:
        return None
    soup = _make_soup(html)
    if soup is not None:
        cards = soup.select("div.s-main-slot div[data-component-type='s-search-result']")
        for card in cards:
            sponsor = card.select_one("span.s-label-popover-default, span.puis-sponsored-label-text")
            if sponsor and "sponsored" in sponsor.get_text(" ", strip=True).lower():
                continue
            link = card.select_one("h2 a")
            if not link:
                continue
            href = link.get("href")
            title = link.get_text(" ", strip=True)
            price_span = card.select_one("span.a-price span.a-offscreen")
            price = parse_money(price_span.get_text(" ", strip=True) if price_span else "")
            if price is None:
                continue
            full_url = urllib.parse.urljoin("https://www.amazon.com", href) if href else None
            if not full_url:
                continue
            asin = (card.get("data-asin") or "").strip().upper() or extract_asin_from_url(href or "")
            return {
                "source": "Amazon",
                "title": title,
                "price": price,
                "shipping": 0.0,
                "total": price,
                "url": full_url,
                "asin": asin if asin else None,
                "pack_qty": detect_pack_qty(title),
            }

    # Regex fallback if BeautifulSoup is unavailable
    card_pattern = re.compile(
        r'data-asin="(?P<asin>[A-Z0-9]{10})"[^>]*>.*?<h2[^>]*>(?P<title>.*?)</h2>.*?<a[^>]+href="(?P<href>[^"]+)"[^>]*>.*?<span[^>]*class="a-offscreen"[^>]*>(?P<price>[^<]+)<',
        re.I | re.S,
    )
    for match in card_pattern.finditer(html):
        block = match.group(0)
        if "Sponsored" in block:
            continue
        title = _strip_html(match.group("title"))
        price = parse_money(match.group("price"))
        if price is None:
            continue
        href = match.group("href")
        full_url = urllib.parse.urljoin("https://www.amazon.com", href)
        asin = (match.group("asin") or "").strip().upper()
        return {
            "source": "Amazon",
            "title": title,
            "price": price,
            "shipping": 0.0,
            "total": price,
            "url": full_url,
            "asin": asin if asin else None,
            "pack_qty": detect_pack_qty(title),
        }
    return None

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

def _parse_ebay_results_from_html(html: str, query: str, condition: str) -> List[Dict]:
    results: List[Dict] = []
    soup = _make_soup(html)
    if soup is not None:
        items = soup.select("li.s-item, div.s-item, div.s-item__wrapper")
        for it in items:
            link = it.select_one("a.s-item__link, a.s-item__title, h3.s-item__title a, a[href*='/itm/']")
            if not link:
                continue
            href = link.get("href")
            url = _unwrap_ebay_url(href)
            if not url:
                continue
            title = link.get_text(" ", strip=True)
            if title and title.lower().startswith("shop on ebay"):
                continue
            badge = it.select_one("span.s-item__ad-badge-text")
            if badge and "sponsored" in badge.get_text(" ", strip=True).lower():
                continue
            price_el = it.select_one("span.s-item__price")
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            if " to " in price_text.lower():
                continue
            price = parse_money(price_text)
            if price is None:
                continue
            ship_el = it.select_one("span.s-item__shipping, span.s-item__logisticsCost")
            ship_text = ship_el.get_text(" ", strip=True) if ship_el else ""
            if ship_text and "free" in ship_text.lower():
                shipping = 0.0
            else:
                shipping = parse_money(ship_text)
            total = price + (shipping if shipping is not None else 0.0)
            cond_el = it.select_one("span.SECONDARY_INFO")
            cond_text = cond_el.get_text(" ", strip=True) if cond_el else ""
            if condition.lower() == "new" and not _is_brand_new_only(cond_text or ""):
                continue
            results.append({
                "source": "eBay",
                "query": query,
                "title": title.strip(),
                "price": price,
                "shipping": shipping,
                "total": total,
                "condition": cond_text.strip(),
                "url": url,
                "has_code": False,
            })
        return results

    pattern = re.compile(
        r'<a[^>]*href="(?P<href>[^"]*/itm/[0-9]+[^"]*)"[^>]*>(?P<title>.*?)</a>.*?s-item__price"[^>]*>(?P<price>[^<]+)</span>.*?(?:s-item__shipping[^>]*>(?P<ship>[^<]*)</span>)?.*?SECONDARY_INFO"[^>]*>(?P<cond>[^<]*)</span>',
        re.I | re.S,
    )
    for match in pattern.finditer(html):
        block = match.group(0)
        if "Sponsored" in block:
            continue
        title = _strip_html(match.group("title"))
        if title.lower().startswith("shop on ebay"):
            continue
        url = _unwrap_ebay_url(match.group("href"))
        if not url:
            continue
        price = parse_money(match.group("price"))
        if price is None:
            continue
        ship_text = match.group("ship") or ""
        if "free" in ship_text.lower():
            shipping = 0.0
        else:
            shipping = parse_money(ship_text)
        total = price + (shipping if shipping is not None else 0.0)
        cond_text = _strip_html(match.group("cond") or "")
        if condition.lower() == "new" and not _is_brand_new_only(cond_text or ""):
            continue
        results.append({
            "source": "eBay",
            "query": query,
            "title": title,
            "price": price,
            "shipping": shipping,
            "total": total,
            "condition": cond_text,
            "url": url,
            "has_code": False,
        })
    return results

async def fetch_ebay_query_http(query: str, condition: str = "new", pages: int = 1) -> List[Dict]:
    query = (query or "").strip()
    if not query:
        return []
    base = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&rt=nc&LH_BIN=1"
    if condition.lower() == "new":
        base += "&LH_ItemCondition=1000"
    results: List[Dict] = []
    for p in range(1, max(1, pages) + 1):
        url = base + (f"&_pgn={p}" if p > 1 else "")
        html = await _http_get(url, EBAY_HEADERS)
        if not html:
            continue
        results.extend(_parse_ebay_results_from_html(html, query, condition))
        if results:
            break
    return results

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
    async with optional_playwright() as play:
        amazon_result: Optional[Dict] = None
        if use_amazon:
            asin_direct = None
            if code and code.startswith("http"):
                asin_direct = extract_asin_from_url(code)
            elif code and re.fullmatch(r"[A-Za-z0-9]{10}", code or ""):
                asin_direct = code.upper()

            if asin_direct and play:
                try:
                    amazon_result = await fetch_amazon_from_asin(play, asin_direct)
                except Exception:
                    amazon_result = None

            if not amazon_result and play and code:
                try:
                    amazon_result = await fetch_amazon_by_search(play, code)
                except Exception:
                    amazon_result = None

            if not amazon_result and play and title:
                try:
                    amazon_result = await fetch_amazon_by_search(play, title)
                except Exception:
                    amazon_result = None

            if not amazon_result and asin_direct:
                try:
                    amazon_result = await fetch_amazon_http_by_asin(asin_direct)
                except Exception:
                    amazon_result = None

            if not amazon_result and code:
                try:
                    amazon_result = await fetch_amazon_http_by_search(code)
                except Exception:
                    amazon_result = None

            if not amazon_result and title:
                try:
                    amazon_result = await fetch_amazon_http_by_search(title)
                except Exception:
                    amazon_result = None

        expected_pack_qty = amazon_result.get("pack_qty") if amazon_result else None

        rows: List[Dict] = []

        queries: List[str] = []
        if code:
            queries.append(code)
            stripped = normalize_upc(code)
            if stripped and stripped != code:
                queries.append(stripped)

        amz_title = (amazon_result or {}).get("title") or title or ""
        base_toks = tokens(amz_title)
        if amz_title:
            variants = [amz_title]
            if expected_pack_qty and expected_pack_qty > 1:
                variants += [
                    f"{amz_title} {expected_pack_qty} pack",
                    f"{amz_title} {expected_pack_qty}-pack",
                    f"{amz_title} {expected_pack_qty}pk",
                    f"pack of {expected_pack_qty} {amz_title}",
                ]
            queries += variants

        for q in queries:
            if len(rows) >= 3:
                break

            play_rows: List[Dict] = []
            if play:
                try:
                    play_rows = await fetch_ebay_query(play, q, condition=condition, visible=visible, pages=pages, retries=retries)
                except Exception:
                    play_rows = []
            if play_rows:
                rows.extend(play_rows)

            if len(rows) < 3:
                try:
                    http_rows = await fetch_ebay_query_http(q, condition=condition, pages=pages)
                except Exception:
                    http_rows = []
                rows.extend(http_rows)

        dedup = {r["url"]: r for r in rows if r.get("url")}
        rows = list(dedup.values())

        filtered: List[Dict] = []
        for r in rows:
            if expected_pack_qty:
                q = detect_pack_qty(r.get("title") or "")
                if q is not None and q != expected_pack_qty:
                    continue
                if q is None and expected_pack_qty > 1:
                    continue
            if base_toks:
                sim = jaccard(base_toks, tokens(r.get("title") or ""))
                if sim < 0.45:
                    continue
            filtered.append(r)

        return {
            "rows": filtered or rows,
            "amazon": amazon_result,
            "meta": {
                "count": len(filtered or rows),
                "expected_pack_qty": expected_pack_qty,
                "normalized_code": normalized_code,
            },
        }

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
