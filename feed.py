#!/usr/bin/env python3
"""Build GRAD's Google Shopping feeds from the Shopify Storefront API.

Replaces the product sync of Shopify's Google & YouTube app, which sends each
country's own domain to Merchant Center once that country moves to one
(gradnordic.fr ...). Merchant Center can claim a single website, so every link
here is built on the one base_url in config.toml.

Writes one XML file per feed label + language, the unit Merchant Center calls a
data source. Two data sources must never carry the same offer ID, label and
language, so a label shared by several countries is one file whose items carry
a shipping line per country.

Reads only public storefront data through the tokenless Storefront API, so the
job needs no Shopify credentials.

    python feed.py --out site                  # feeds, report.md, report.json, index.html
    python feed.py --out site --check-links    # also open one landing page per feed

Exit codes: 0 = written, clean or with warnings. 2 = written, but there are
errors: items left out or a landing page that disagrees with the feed (see
report). 1 = nothing written, so the last good feeds stay live.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import string
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
UA = "GRAD-feed/1.0 (+https://github.com/GRAD-APS/grad-feed)"
# Merchant Center says "pt"; Shopify publishes Portuguese as pt-PT.
SHOPIFY_LANG = {"pt": "PT_PT"}
# Language folders on the storefront; English is the root.
FOLDER = {"en": ""}
# Every value excluded_destination accepts (Merchant Center product data spec).
DESTINATIONS = ["Shopping_ads", "Display_ads", "Local_inventory_ads", "Free_listings",
                "Free_local_listings", "Cloud_retail", "Local_cloud_retail",
                "Youtube_affiliate", "Youtube_merchandise"]
LINK_FIELDS = {"base_url", "folder", "handle", "variant", "country"}

QUERY = """
query Feed($country: CountryCode!, $language: LanguageCode!, $after: String)
@inContext(country: $country, language: $language) {
  localization { country { isoCode currency { isoCode } } language { isoCode } }
  products(first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle title descriptionHtml onlineStoreUrl
      images(first: 20) { nodes { url } }
      variants(first: 100) { nodes {
        id title sku barcode availableForSale currentlyNotInStock
        price { amount currencyCode }
        compareAtPrice { amount currencyCode }
        image { url }
      } }
    }
  }
}"""


class Fatal(Exception):
    """Wrong enough that no feed may be published."""


# --- config -----------------------------------------------------------------

def load_config(path):
    cfg = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("shop", "api_version", "base_url", "brand", "offer_id_format", "countries"):
        if not cfg.get(key):
            raise Fatal(f"config: {key} is missing")
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    for cc, c in cfg["countries"].items():
        if not re.fullmatch(r"[A-Z]{2}", cc):
            raise Fatal(f"config: {cc!r} is not a two-letter country code")
        missing = [k for k in ("languages", "currency", "feed_label", "link") if not c.get(k)]
        if missing:
            raise Fatal(f"config: {cc} is missing {', '.join(missing)}")
        fields = {f for _, f, _, _ in string.Formatter().parse(link_pattern(cfg, cc)) if f}
        if not {"handle", "variant"} <= fields <= LINK_FIELDS:
            raise Fatal(f"config: {cc} link must use {{handle}} and {{variant}} and only "
                        f"{', '.join(sorted(LINK_FIELDS))}")
        feed_label(cfg, c["feed_label"])
    cfg["zone_of"] = shipping_zones(cfg)
    return cfg


def shipping_zones(cfg):
    """Country -> (zone, rate, currency): the Shopify zone that contains it."""
    zone_of = {}
    for name, z in cfg.get("shipping_zones", {}).items():
        for cc in z.get("countries", []):
            if cc in zone_of:
                raise Fatal(f"config: {cc} is in two shipping zones, {zone_of[cc][0]} and {name}")
            zone_of[cc] = (name, Decimal(str(z["rate"])).quantize(Decimal("0.01")), z.get("currency", "EUR"))
    missing = [cc for cc in cfg["countries"] if cc not in zone_of]
    if missing:
        raise Fatal(f"config: no shipping zone contains {', '.join(missing)}, so checkout can't deliver "
                    f"there. Add the zone from Shopify or remove the country")
    return zone_of


def link_pattern(cfg, cc):
    """A country's link pattern: a [link_styles] name or a literal pattern."""
    link = cfg["countries"][cc]["link"]
    return cfg.get("link_styles", {}).get(link, link)


def feed_label(cfg, label):
    """The label the Merchant Center data source carries (TEST_ in test mode)."""
    if cfg.get("test_mode"):
        label = "TEST_" + re.sub(r"^[A-Z]{3}_", "", label)
    if not re.fullmatch(r"[A-Z0-9_-]{1,20}", label):
        raise Fatal(f"config: feed label {label!r} breaks Merchant Center's rule "
                    f"(A-Z, 0-9, - and _, at most 20 characters)")
    return label


def offer_id(cfg, oid):
    return f"TEST_{oid}" if cfg.get("test_mode") else oid


def feed_groups(cfg):
    """(feed label, language) -> countries in config order; the first anchors links."""
    groups = defaultdict(list)
    for cc, c in cfg["countries"].items():
        for lang in c["languages"]:
            groups[(c["feed_label"], lang)].append(cc)
    return groups


# --- Shopify ----------------------------------------------------------------

def storefront(cfg, variables, retries=4):
    url = f"https://{cfg['shop']}/api/{cfg['api_version']}/graphql.json"
    body = json.dumps({"query": QUERY, "variables": variables}).encode()
    err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        else:
            if "errors" not in data:
                return data["data"]
            err = data["errors"]
            if not any(e.get("extensions", {}).get("code") == "THROTTLED" for e in err):
                break
        time.sleep(2 ** attempt)
    raise Fatal(f"Storefront API {variables['country']}/{variables['language']}: {str(err)[:300]}")


def fetch(cfg, country, lang):
    """All products as Shopify serves them in one country and language."""
    products, after = [], None
    while True:
        d = storefront(cfg, {"country": country,
                             "language": SHOPIFY_LANG.get(lang, lang.upper()),
                             "after": after})
        products += d["products"]["nodes"]
        page = d["products"]["pageInfo"]
        if not page["hasNextPage"]:
            return d["localization"], products
        after = page["endCursor"]


def landing(url, retries=4):
    """Fetch a storefront page: (final url, active currency, rate from EUR, html)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read().decode("utf-8", "replace")
                final = r.geturl()
            break
        except urllib.error.HTTPError as e:
            # Shopify answers bursts of page loads with 429/503; back off.
            if e.code not in (429, 502, 503) or attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    m = re.search(r'Shopify\.currency\s*=\s*\{"active":"([A-Z]{3})","rate":"([0-9.]+)"', page)
    return final, (m.group(1) if m else None), (Decimal(m.group(2)) if m else None), page


# --- text -------------------------------------------------------------------

BLOCK_TAG = re.compile(r"</?(?:p|div|h[1-6]|li|ul|ol|br|tr|table|section|blockquote)\b[^>]*>", re.I)
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def html_to_lines(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.I | re.S)
    s = html.unescape(re.sub(r"<[^>]+>", " ", BLOCK_TAG.sub("\n", s)))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in s.split("\n"))
    return [line for line in lines if line]


class AlcoholFilter:
    def __init__(self, cfg):
        a = cfg.get("alcohol", {})
        self.allow = tuple(w.lower() for w in a.get("allow", []))
        common = a.get("all", [])
        self.patterns = defaultdict(lambda: [re.compile(p, re.I) for p in common])
        for lang, pats in a.items():
            if lang not in ("all", "allow"):
                self.patterns[lang] = [re.compile(p, re.I) for p in common + pats]

    def hits(self, text, lang):
        return [w for w in re.findall(r"\w+", text)
                if not w.lower().startswith(self.allow)
                and any(p.fullmatch(w) for p in self.patterns[lang])]

    def clean(self, lines, lang):
        """Drop every sentence with alcohol wording: (kept lines, dropped sentences)."""
        kept, dropped = [], []
        for line in lines:
            sentences = re.split(r"(?<=[.!?…])\s+", line)
            ok = [s for s in sentences if not self.hits(s, lang)]
            dropped += [s for s in sentences if self.hits(s, lang)]
            if ok:
                kept.append(" ".join(ok))
        return kept, dropped


# --- records ----------------------------------------------------------------

def gid(s):
    return s.rsplit("/", 1)[-1]


def money(m):
    return Decimal(m["amount"]).quantize(Decimal("0.01")) if m else None


def records(cfg, filt, lang, products):
    """One record per variant, keyed by the real offer ID, for one country + language."""
    out = {}
    for p in products:
        lines, dropped = filt.clean(html_to_lines(p["descriptionHtml"]), lang)
        images = [i["url"] for i in p["images"]["nodes"]]
        variants = p["variants"]["nodes"]
        for v in variants:
            pid, vid = gid(p["id"]), gid(v["id"])
            main = (v.get("image") or {}).get("url") or (images[0] if images else None)
            price, compare = money(v["price"]), money(v["compareAtPrice"])
            on_sale = bool(compare and price is not None and compare > price)
            oid = cfg["offer_id_format"].format(product_id=pid, variant_id=vid)
            out[oid] = {
                "offer_id": oid,
                "product_id": pid,
                "variant_id": vid,
                "handle": p["handle"],
                "group": pid if len(variants) > 1 else None,
                "title": p["title"] if len(variants) == 1 else f"{p['title']} – {v['title']}",
                "description": "\n".join(lines)[:5000],
                "dropped": dropped,
                "on_store": bool(p["onlineStoreUrl"]),
                "image": main,
                "more_images": [u for u in images if u != main][:cfg.get("max_additional_images", 10)],
                "available": v["availableForSale"],
                "zero_stock": v["currentlyNotInStock"],
                "price": compare if on_sale else price,
                "sale_price": price if on_sale else None,
                "currency": v["price"]["currencyCode"],
                "sku": v["sku"] or None,
                "barcode": (v["barcode"] or "").strip() or None,
            }
    return out


# --- run --------------------------------------------------------------------

class Run:
    def __init__(self, cfg, check_links=False):
        self.cfg = cfg
        self.check_links = check_links
        self.filt = AlcoholFilter(cfg)
        self.groups = feed_groups(cfg)
        self.errors, self.warnings = [], []
        self.data = {}        # (country, lang) -> {offer id: record}
        self.rates = {}       # country -> EUR exchange rate on the storefront
        self.feeds = []       # summaries for the report
        self.dropped = {}     # (lang, title) -> dropped sentences
        self.links = []       # landing page checks

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def fetch_all(self):
        pairs = [(cc, lang) for cc, c in self.cfg["countries"].items() for lang in c["languages"]]
        with cf.ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {pool.submit(fetch, self.cfg, cc, lang): (cc, lang) for cc, lang in pairs}
            for job in cf.as_completed(jobs):
                cc, lang = jobs[job]
                loc, products = job.result()
                self.check_context(cc, lang, loc, products)
                self.data[(cc, lang)] = records(self.cfg, self.filt, lang, products)
        if not any(self.data.values()):
            raise Fatal("Shopify returned no products at all")

    def check_context(self, cc, lang, loc, products):
        want_lang = SHOPIFY_LANG.get(lang, lang.upper())
        currency = self.cfg["countries"][cc]["currency"]
        if loc["country"]["isoCode"] != cc:
            raise Fatal(f"{cc}: Shopify answered as {loc['country']['isoCode']}; is {cc} in a market?")
        if loc["language"]["isoCode"] != want_lang:
            raise Fatal(f"{cc}/{lang}: Shopify does not publish {lang} for {cc} "
                        f"(it served {loc['language']['isoCode']}); remove it from config")
        if loc["country"]["currency"]["isoCode"] != currency:
            raise Fatal(f"{cc}: config says {currency}, Shopify sells in "
                        f"{loc['country']['currency']['isoCode']}. Change the Shopify market "
                        f"or the currency in config.toml so they agree")
        for p in products:
            if not p["onlineStoreUrl"]:
                self.warn(f"'{p['title']}' is not on the Online Store, so it has no page to link to; left out")

    def shipping(self, cc, currency):
        """Shipping for one country in the feed currency, from the zone containing it, or None."""
        _, amount, source = self.cfg["zone_of"][cc]
        if source == currency or amount == 0:
            return amount
        if source != "EUR":
            self.warn(f"{cc}: cannot convert {source} shipping to {currency}; shipping left out")
            return None
        if cc not in self.rates:
            try:
                _, active, rate, _ = landing(f"{self.cfg['base_url']}/?country={cc}")
            except Exception as e:  # noqa: BLE001 - reported, never fatal
                active, rate = None, None
                self.warn(f"{cc}: could not read the exchange rate ({e})")
            self.rates[cc] = rate if active == currency else None
        if self.rates[cc] is None:
            self.warn(f"{cc}: no {currency} exchange rate on the storefront; shipping left out")
            return None
        return (amount * self.rates[cc]).quantize(Decimal("0.01"))

    def build(self):
        cfg = self.cfg
        english = {}
        for (cc, lang), recs in self.data.items():
            if lang == "en":
                for oid, r in recs.items():
                    english.setdefault(oid, r["description"])
        paused = set(cfg.get("paused", []))
        standard = {}   # offer -> (price, sale, currency, feed) across "folder" links

        for (label, lang), countries in self.groups.items():
            anchor = countries[0]
            patterns = {link_pattern(cfg, cc) for cc in countries}
            if len(patterns) > 1:
                raise Fatal(f"{label}/{lang}: {', '.join(countries)} share a feed but not a link rule")
            pattern = patterns.pop()
            folder_rule = all(cfg["countries"][cc]["link"] == "folder" for cc in countries)
            currency = cfg["countries"][anchor]["currency"]
            ship = {cc: self.shipping(cc, currency) for cc in countries}
            offers = {oid for cc in countries for oid in self.data[(cc, lang)]}
            items, skipped = [], 0
            for oid in sorted(offers):
                present = {cc: self.data[(cc, lang)][oid] for cc in countries if oid in self.data[(cc, lang)]}
                rec = present.get(anchor) or next(iter(present.values()))
                where = f"{label}/{lang} {rec['title']}"

                prices = {(r["price"], r["sale_price"], r["currency"]) for r in present.values()}
                if len(prices) > 1:
                    raise Fatal(f"{where}: countries sharing this label are priced differently "
                                f"({sorted(map(str, prices))}); one item can carry one price. "
                                f"Give them separate feed labels")
                if folder_rule:
                    # No ?country= in the link, so the page shows the standard price.
                    key = (rec["price"], rec["sale_price"], rec["currency"])
                    first = standard.setdefault(oid, (*key, f"{label}/{lang}"))
                    if first[:3] != key:
                        raise Fatal(f"{where}: {key[0]} {key[2]}, but {first[3]} has {first[0]} {first[2]}. "
                                    f"Links without ?country= all show one standard price")
                if len({r["available"] for r in present.values()}) > 1:
                    self.warn(f"{where}: sold out in some of {', '.join(present)} only; feed uses {anchor}")

                problems = []
                if rec["price"] is None:
                    problems.append("no price")
                if not rec["image"]:
                    problems.append("no image")
                if not rec["on_store"]:
                    problems.append("no Online Store page")
                if hits := self.filt.hits(rec["title"], lang):
                    problems.append(f"alcohol wording in the title ({', '.join(hits)})")
                if not rec["description"]:
                    problems.append("description empty after the alcohol filter")
                if problems:
                    self.errors.append(f"{where}: left out, {'; '.join(problems)}")
                    skipped += 1
                    continue

                if lang != "en" and rec["description"] == english.get(oid):
                    self.warn(f"{rec['title']}: description not translated to {lang}; English is sent")
                if rec["dropped"]:
                    self.dropped[(lang, rec["title"])] = rec["dropped"]

                link = pattern.format(base_url=cfg["base_url"], folder=FOLDER.get(lang, "/" + lang),
                                      handle=rec["handle"], variant=rec["variant_id"], country=anchor)
                items.append({**rec,
                              "id": offer_id(cfg, oid),
                              "link": link,
                              "shipping": [(cc, ship[cc]) for cc in present if ship[cc] is not None],
                              "excluded": [cc for cc in countries if cc not in present],
                              "paused": f"{label}/{lang}/{oid}" in paused})

            mc_label = feed_label(cfg, label)
            self.feeds.append({"file": f"{mc_label}.{lang}.xml", "label": mc_label, "app_label": label,
                               "language": lang, "currency": currency, "countries": countries,
                               "anchor": anchor, "items": items, "skipped": skipped})

    def verify_links(self):
        """Open the first item of every feed and compare it with the feed."""
        host = urllib.parse.urlsplit(self.cfg["base_url"]).netloc
        for f in self.feeds:
            if not f["items"]:
                continue
            it = f["items"][0]
            result = {"file": f["file"], "url": it["link"]}
            try:
                final, active, _, page = landing(it["link"])
            except urllib.error.HTTPError as e:
                result["problem"] = f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001
                result["problem"] = str(e)[:200]
            else:
                amount = it["sale_price"] or it["price"]
                problems = []
                if urllib.parse.urlsplit(final).netloc != host:
                    problems.append(f"redirects off {host} to {final}")
                elif final.split("?")[0] != it["link"].split("?")[0]:
                    problems.append(f"redirects to {final}")
                if active != f["currency"]:
                    problems.append(f"page shows {active}, feed says {f['currency']}")
                if not re.search(r'"price"\s*:\s*"?%s' % re.escape(str(amount)), page):
                    problems.append(f"{amount} not found on the page")
                result.update(final=final, currency=active)
                if problems:
                    result["problem"] = "; ".join(problems)
            if "problem" in result:
                self.errors.append(f"{f['file']}: landing page check failed: {result['problem']} ({it['link']})")
            self.links.append(result)

    def missing_fields(self):
        """Recommended attributes Shopify has no value for, per offer."""
        missing = defaultdict(set)
        for f in self.feeds:
            for it in f["items"]:
                if not it["barcode"]:
                    missing["gtin"].add(it["title"])
                if not it["sku"]:
                    missing["mpn (Shopify SKU)"].add(it["title"])
                if not it["more_images"]:
                    missing["additional_image_link"].add(it["title"])
        return {k: sorted(v) for k, v in missing.items()}


# --- output -----------------------------------------------------------------

def el(tag, value):
    return f"<g:{tag}>{escape(INVALID_XML.sub('', str(value)))}</g:{tag}>"


def fmt_price(amount, currency):
    return f"{amount} {currency}"


def write_feed(path, cfg, f):
    test = cfg.get("test_mode")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
           "<channel>",
           f"<title>{escape(('TEST ' if test else '') + 'GRAD ' + f['label'] + ' ' + f['language'])}</title>",
           f"<link>{escape(cfg['base_url'])}</link>",
           f"<description>{escape('Countries: ' + ', '.join(f['countries']))}</description>"]
    for it in f["items"]:
        out.append("<item>")
        out += [el("id", it["id"]), el("title", it["title"][:150]),
                el("description", it["description"]), el("link", it["link"]),
                el("image_link", it["image"])]
        out += [el("additional_image_link", u) for u in it["more_images"]]
        out += [el("availability", "in_stock" if it["available"] else "out_of_stock"),
                el("price", fmt_price(it["price"], f["currency"]))]
        if it["sale_price"]:
            out.append(el("sale_price", fmt_price(it["sale_price"], f["currency"])))
        out.append(el("brand", cfg["brand"]))
        if it["barcode"]:
            out.append(el("gtin", it["barcode"]))
        if it["sku"]:
            out.append(el("mpn", it["sku"]))
        if not it["barcode"] and not it["sku"]:
            out.append(el("identifier_exists", "no"))
        out += [el("condition", cfg.get("condition", "new")),
                el("google_product_category", cfg["google_product_category"])]
        if it["group"]:
            out.append(el("item_group_id", it["group"]))
        for cc, amount in it["shipping"]:
            out.append(f"<g:shipping>{el('country', cc)}{el('price', fmt_price(amount, f['currency']))}</g:shipping>")
        out += [el("shopping_ads_excluded_country", cc) for cc in it["excluded"]]
        if test:
            out += [el("excluded_destination", d) for d in DESTINATIONS]
        if it["paused"]:
            out.append(el("pause", "ads"))
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    path.write_text("\n".join(out), encoding="utf-8")


def fatal_report(started, msg):
    return (f"# GRAD Shopping feed: {started:%Y-%m-%d %H:%M} UTC\n\n"
            f"**Nothing published.** The last good feeds stay live.\n\n> {msg}\n")


def report(run, started):
    cfg = run.cfg
    pages = cfg.get("pages_url", "").rstrip("/")
    lines = [f"# GRAD Shopping feed: {started:%Y-%m-%d %H:%M} UTC", ""]
    if cfg.get("test_mode"):
        lines += ["**TEST MODE**: TEST_ offer IDs and feed labels, every item excluded from "
                  "every destination. Nothing can show in ads or free listings.", ""]
    n_items = sum(len(f["items"]) for f in run.feeds)
    lines += [f"Links on **{cfg['base_url']}** · {len(run.feeds)} feeds · {len(cfg['countries'])} countries "
              f"· {n_items} items · **{len(run.errors)} errors** · {len(run.warnings)} warnings", ""]
    if run.errors:
        lines += ["## Errors", ""] + [f"- {e}" for e in run.errors] + [""]
    if run.warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in run.warnings] + [""]

    if run.links:
        ok = sum(1 for r in run.links if "problem" not in r)
        lines += ["## Landing pages", "", f"{ok} of {len(run.links)} matched the feed "
                  f"(same domain and path, same currency, same price).", "",
                  "| Feed | Link checked | Page currency | Result |", "|---|---|---|---|"]
        lines += [f"| {r['file']} | {r['url']} | {r.get('currency') or '–'} | {r.get('problem', 'ok')} |"
                  for r in run.links]
        lines.append("")

    missing = run.missing_fields()
    lines += ["## Missing fields", ""]
    lines += [f"- **{field}**: {', '.join(titles)}" for field, titles in missing.items()] or ["None."]
    lines.append("")

    lines += ["## Items per country", "",
              "| Country | Feed label | Languages | Currency | Items | In stock | Shipping (zone) | Prices |",
              "|---|---|---|---|---|---|---|---|"]
    by_country = defaultdict(dict)
    for f in run.feeds:
        for it in f["items"]:
            for cc in f["countries"]:
                if cc not in it["excluded"]:
                    by_country[cc][it["offer_id"]] = it
    for cc, c in cfg["countries"].items():
        offers = by_country.get(cc, {})
        stock = sum(1 for it in offers.values() if it["available"])
        ships = {str(dict(it["shipping"]).get(cc, "–")) for it in offers.values()} or {"–"}
        prices = " / ".join(f"{it['title'].split(' - ')[-1]} {it['sale_price'] or it['price']}"
                            for it in sorted(offers.values(), key=lambda x: x["title"]))
        lines.append(f"| {cc} | {feed_label(cfg, c['feed_label'])} | {', '.join(c['languages'])} | {c['currency']} "
                     f"| {len(offers)} | {stock} | {', '.join(sorted(ships))} ({cfg['zone_of'][cc][0]}) | {prices} |")
    lines.append("")

    lines += ["## Feeds", "", "One Merchant Center data source each: fetch URL, feed label, content language, countries.", "",
              "| Fetch URL | Feed label | Language | Countries | Items |", "|---|---|---|---|---|"]
    for f in run.feeds:
        url = f"{pages}/{f['file']}" if pages else f["file"]
        skipped = f" ({f['skipped']} left out)" if f["skipped"] else ""
        lines.append(f"| {url} | {f['label']} | {f['language']} | {', '.join(f['countries'])} "
                     f"| {len(f['items'])}{skipped} |")
    lines.append("")

    if run.dropped:
        lines += ["## Removed by the alcohol filter", "", "<details><summary>Sentences dropped from descriptions</summary>", ""]
        for (lang, title), sentences in sorted(run.dropped.items()):
            lines.append(f"**{title}** ({lang})")
            lines += [f"- {s}" for s in sentences]
            lines.append("")
        lines += ["</details>", ""]
    return "\n".join(lines)


def index_html(run, started):
    test = run.cfg.get("test_mode")
    rows = "\n".join(
        f"<tr><td><a href=\"{f['file']}\">{f['file']}</a></td><td>{f['label']}</td><td>{f['language']}</td>"
        f"<td>{', '.join(f['countries'])}</td><td>{len(f['items'])}</td></tr>" for f in run.feeds)
    banner = ("<p class=\"test\">TEST MODE: TEST_ offer IDs and feed labels; every item is excluded "
              "from every destination.</p>") if test else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRAD Shopping feeds</title>
<style>
:root{{--bg:#faf9f7;--fg:#1c1b19;--muted:#6b675f;--line:#e4e0d8;--warn:#8a5a00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161514;--fg:#ece9e3;--muted:#a19c92;--line:#33312d;--warn:#e0b45c}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:24px 16px}}
h1{{font-weight:300;letter-spacing:.02em}} p{{color:var(--muted)}} a{{color:inherit}} .test{{color:var(--warn);font-weight:600}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
</style></head><body>
<h1>GRAD Shopping feeds</h1>
{banner}
<p>Built {started:%Y-%m-%d %H:%M} UTC from Shopify. Links on {escape(run.cfg['base_url'])}.
One file per Merchant Center data source. <a href="report.md">Run report</a></p>
<div class="wrap"><table><tr><th>File</th><th>Feed label</th><th>Language</th><th>Countries</th><th>Items</th></tr>
{rows}
</table></div></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=ROOT / "config.toml")
    ap.add_argument("--out", default=ROOT / "site", type=Path)
    ap.add_argument("--check-links", action="store_true",
                    help="open one landing page per feed and compare domain, currency and price")
    args = ap.parse_args(argv)

    started = dt.datetime.now(dt.timezone.utc)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        run = Run(load_config(args.config), args.check_links)
        run.fetch_all()
        run.build()
        if run.check_links:
            run.verify_links()
    except Fatal as e:
        text = fatal_report(started, e)
        (args.out / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 1

    for old in args.out.glob("*.xml"):
        old.unlink()
    for f in run.feeds:
        write_feed(args.out / f["file"], run.cfg, f)
    text = report(run, started)
    (args.out / "report.md").write_text(text, encoding="utf-8")
    (args.out / "index.html").write_text(index_html(run, started), encoding="utf-8")
    (args.out / "report.json").write_text(json.dumps({
        "built": started.isoformat(), "base_url": run.cfg["base_url"],
        "test_mode": bool(run.cfg.get("test_mode")),
        "errors": run.errors, "warnings": run.warnings, "missing_fields": run.missing_fields(),
        "feeds": [{k: v for k, v in f.items() if k != "items"} | {"items": len(f["items"])} for f in run.feeds],
        "links": run.links,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(text)
    return 2 if run.errors else 0


if __name__ == "__main__":
    sys.exit(main())
