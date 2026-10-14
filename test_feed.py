"""Offline tests: python -m unittest -v"""
import copy
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import feed

HERE = Path(__file__).parent
CFG = feed.load_config(HERE / "config.toml")
G = "{http://base.google.com/ns/1.0}"


def product(title="Grad No.1 - 2026 Steel Series", body="<p>Quiet.</p>", price="699.0",
            compare=None, available=True, sku="GRAD-NO1-STEEL", barcode=None):
    return {
        "id": "gid://shopify/Product/8588622201170", "handle": "grad-no-1-steel-series",
        "title": title, "descriptionHtml": body,
        "onlineStoreUrl": "https://grad.dk/products/grad-no-1-steel-series",
        "images": {"nodes": [{"url": "https://cdn.shopify.com/a.jpg"}, {"url": "https://cdn.shopify.com/b.jpg"}]},
        "variants": {"nodes": [{
            "id": "gid://shopify/ProductVariant/47061726298450", "title": "Default Title",
            "sku": sku, "barcode": barcode, "availableForSale": available, "currentlyNotInStock": False,
            "price": {"amount": price, "currencyCode": "EUR"},
            "compareAtPrice": {"amount": compare, "currencyCode": "EUR"} if compare else None,
            "image": None}]},
    }


def config(**overrides):
    cfg = copy.deepcopy(CFG)
    cfg.update(overrides)
    return cfg


def run_for(cfg, groups, lang="en", **kw):
    run = feed.Run(cfg)
    run.groups = groups
    recs = feed.records(cfg, run.filt, lang, [product(**kw)])
    run.data = {(cc, lang): recs for countries in groups.values() for cc in countries}
    run.build()
    return run


def first_item(cfg, f):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f["file"]
        feed.write_feed(path, cfg, f)
        return ET.parse(path).getroot().find("channel/item")


class AlcoholFilterTest(unittest.TestCase):
    f = feed.AlcoholFilter(CFG)

    def test_drops_only_the_sentence_with_alcohol_wording(self):
        kept, dropped = self.f.clean(["Silent. Built for the ultimate wine experience. No ice."], "en")
        self.assertEqual(kept, ["Silent. No ice."])
        self.assertEqual(dropped, ["Built for the ultimate wine experience."])

    def test_catches_compounds(self):
        self.assertTrue(self.f.hits("Der moderne Weinkeller", "de"))
        self.assertTrue(self.f.hits("den moderne vinkælder", "da"))
        self.assertTrue(self.f.hits("as the winemaker intended", "en"))

    def test_leaves_lookalikes(self):
        self.assertFalse(self.f.hits("Stil den i vinduet om vinteren", "da"))
        self.assertFalse(self.f.hits("living room, vintage table", "en"))
        self.assertFalse(self.f.hits("Kühlung ohne Eis", "de"))

    def test_title_with_alcohol_wording_leaves_item_out(self):
        run = run_for(CFG, {("EUR_101523849554", "en"): ["DK"]}, title="GRAD Wine Cooler")
        self.assertEqual(run.feeds[0]["items"], [])
        self.assertIn("alcohol wording in the title", run.errors[0])


class RecordTest(unittest.TestCase):
    def rec(self, **kw):
        return next(iter(feed.records(CFG, feed.AlcoholFilter(CFG), "en", [product(**kw)]).values()))

    def test_offer_id_matches_the_app(self):
        self.assertEqual(self.rec()["offer_id"], "shopify_ZZ_8588622201170_47061726298450")

    def test_sale_price_when_compare_at_is_higher(self):
        r = self.rec(price="599.0", compare="699.0")
        self.assertEqual((r["price"], r["sale_price"]), (Decimal("699.00"), Decimal("599.00")))

    def test_no_sale_when_compare_at_is_not_higher(self):
        r = self.rec(price="699.0", compare="699.0")
        self.assertEqual((r["price"], r["sale_price"]), (Decimal("699.00"), None))

    def test_html_becomes_lines(self):
        lines = feed.html_to_lines("<h4>RAW&nbsp;ELEGANCE</h4><p>Steel.<br>Silent &amp; cordless.</p><ul><li>5–20°C</li></ul>")
        self.assertEqual(lines, ["RAW ELEGANCE", "Steel.", "Silent & cordless.", "5–20°C"])


class LinkTest(unittest.TestCase):
    def link(self, cfg, cc, lang):
        run = run_for(cfg, {(cfg["countries"][cc]["feed_label"], lang): [cc]}, lang=lang)
        return run.feeds[0]["items"][0]["link"]

    BASE = CFG["base_url"]
    PATH = "/products/grad-no-1-steel-series?variant=47061726298450"

    def test_euro_countries_use_the_language_folder_and_no_country(self):
        self.assertEqual(self.link(CFG, "FR", "fr"), f"{self.BASE}/fr{self.PATH}")
        self.assertEqual(self.link(CFG, "DE", "en"), f"{self.BASE}{self.PATH}")
        self.assertEqual(self.link(CFG, "DK", "da"), f"{self.BASE}/da{self.PATH}")

    def test_market_subfolders(self):
        self.assertEqual(self.link(CFG, "US", "en"), f"{self.BASE}/en-us{self.PATH}")
        self.assertEqual(self.link(CFG, "CA", "en"), f"{self.BASE}/en-ca{self.PATH}")

    def test_main_domain_countries_keep_country(self):
        self.assertEqual(self.link(CFG, "CH", "en"), f"{self.BASE}{self.PATH}&country=CH")
        self.assertEqual(self.link(CFG, "SE", "en"), f"{self.BASE}{self.PATH}&country=SE")
        self.assertTrue(self.link(CFG, "JP", "en").endswith("&country=JP"))

    def test_folder_countries_use_the_base_domain_context(self):
        self.assertEqual(feed.context_country(CFG, "DK"), "BM")
        self.assertEqual(feed.context_country(CFG, "FR"), "BM")
        self.assertEqual(feed.context_country(CFG, "SE"), "SE")
        self.assertEqual(feed.context_country(CFG, "US"), "US")

    def test_live_config(self):
        self.assertEqual(self.BASE, "https://gradnordic.com")
        self.assertFalse(CFG["test_mode"])

    def test_euro_countries_must_share_the_standard_price(self):
        cfg = config()
        run = feed.Run(cfg)
        run.groups = {("EUR_101523849554", "en"): ["DK"], ("EUR_62471242066", "en"): ["FR"]}
        run.data = {("DK", "en"): feed.records(cfg, run.filt, "en", [product(price="699.0")]),
                    ("FR", "en"): feed.records(cfg, run.filt, "en", [product(price="749.0")])}
        with self.assertRaises(feed.Fatal):
            run.build()

    def test_shared_feed_needs_one_link_rule(self):
        cfg = config()
        cfg["countries"] = {**cfg["countries"], "BG": {**cfg["countries"]["BG"], "link": "country"}}
        with self.assertRaises(feed.Fatal):
            run_for(cfg, {("EUR_62471242066", "en"): ["FR", "BG"]})


class FeedTest(unittest.TestCase):
    def test_one_item_carries_shipping_per_country(self):
        cfg = config(test_mode=False)
        f = run_for(cfg, {("EUR_62471242066", "en"): ["FR", "GR"]}).feeds[0]
        item = first_item(cfg, f)
        self.assertEqual(f["file"], "EUR_62471242066.en.xml")
        self.assertEqual(item.find(G + "id").text, "shopify_ZZ_8588622201170_47061726298450")
        self.assertEqual(item.find(G + "price").text, "699.00 EUR")
        self.assertEqual(item.find(G + "brand").text, "GRAD")
        self.assertEqual(item.find(G + "google_product_category").text, "4539")
        ship = {s.find(G + "country").text: s.find(G + "price").text for s in item.findall(G + "shipping")}
        self.assertEqual(ship, {"FR": "0.00 EUR", "GR": "89.00 EUR"})
        self.assertIsNone(item.find(G + "gtin"))
        self.assertEqual(item.find(G + "mpn").text, "GRAD-NO1-STEEL")
        self.assertEqual(item.findall(G + "excluded_destination"), [])

    def test_test_mode_prefixes_and_excludes_everything(self):
        cfg = config(test_mode=True)
        f = run_for(cfg, {("EUR_101523849554", "en"): ["DK"]}).feeds[0]
        item = first_item(cfg, f)
        self.assertEqual(f["label"], "TEST_101523849554")
        self.assertEqual(f["file"], "TEST_101523849554.en.xml")
        self.assertEqual(item.find(G + "id").text, "TEST_shopify_ZZ_8588622201170_47061726298450")
        self.assertEqual({e.text for e in item.findall(G + "excluded_destination")}, set(feed.DESTINATIONS))

    def test_every_test_label_fits_merchant_center(self):
        cfg = config(test_mode=True)
        for c in cfg["countries"].values():
            self.assertLessEqual(len(feed.feed_label(cfg, c["feed_label"])), 20)

    def test_availability_follows_shopify(self):
        f = run_for(CFG, {("EUR_101523849554", "en"): ["DK"]}, available=False).feeds[0]
        self.assertFalse(f["items"][0]["available"])

    def test_shared_label_with_different_prices_stops_the_run(self):
        run = feed.Run(CFG)
        run.groups = {("EUR_109522649426", "en"): ["AE", "SA"]}
        run.data = {("AE", "en"): feed.records(CFG, run.filt, "en", [product(price="550.0")]),
                    ("SA", "en"): feed.records(CFG, run.filt, "en", [product(price="600.0")])}
        with self.assertRaises(feed.Fatal):
            run.build()


class ShippingZoneTest(unittest.TestCase):
    def zones(self, zones, countries=("JP",)):
        raw = tomllib.loads((HERE / "config.toml").read_text(encoding="utf-8"))
        raw["shipping_zones"] = zones
        raw["countries"] = {cc: raw["countries"][cc] for cc in countries}
        return feed.shipping_zones(raw)

    def test_each_country_takes_its_own_zone(self):
        self.assertEqual(CFG["zone_of"]["JP"], ("Japan", Decimal("140.00"), "EUR"))
        self.assertEqual(CFG["zone_of"]["SG"][0], "Asia")
        self.assertEqual(CFG["zone_of"]["US"], ("USA", Decimal("39.00"), "USD"))
        self.assertEqual(CFG["zone_of"]["GR"][1], Decimal("89.00"))

    def test_japan_feed_ships_at_the_japan_rate(self):
        f = run_for(CFG, {("EUR_115746111826", "en"): ["JP"]}).feeds[0]
        ship = {s.find(G + "country").text: s.find(G + "price").text
                for s in first_item(CFG, f).findall(G + "shipping")}
        self.assertEqual(ship, {"JP": "140.00 EUR"})

    def test_country_in_no_zone_stops_the_run(self):
        with self.assertRaises(feed.Fatal):
            self.zones({"Asia": {"rate": 39, "countries": ["SG"]}})

    def test_country_in_two_zones_stops_the_run(self):
        with self.assertRaises(feed.Fatal):
            self.zones({"Japan": {"rate": 140, "countries": ["JP"]},
                        "Asia": {"rate": 39, "countries": ["JP", "SG"]}})


class ConfigTest(unittest.TestCase):
    def test_uk_is_out(self):
        self.assertNotIn("GB", CFG["countries"])

    def test_live_pmax_label_is_kept(self):
        self.assertEqual(CFG["countries"]["FR"]["feed_label"], "EUR_62471242066")

    def test_links_never_use_a_country_domain(self):
        self.assertNotRegex(CFG["base_url"], r"gradnordic\.(?!com)")

    def test_labels_are_the_apps(self):
        labels = {cc: c["feed_label"] for cc, c in CFG["countries"].items()}
        app = {"EUR_62471242066": "FR BE NL ES PT LU LI BG CY CZ EE FI GE GR HR HU IE LT LV MT NO PL RO SK TR UA",
               "EUR_101524046162": "DE AT", "EUR_104670429522": "IT", "EUR_101523849554": "DK",
               "USD_92753658194": "US", "EUR_115746111826": "JP", "EUR_109522649426": "AE SA KW OM BH",
               "EUR_370737387": "ZA IL LB BD JO KZ MM MU NP PK UZ", "CHF_119513284946": "CH",
               "SEK_119513022802": "SE", "CAD_113515200850": "CA", "AU": "AU", "NZ": "NZ",
               **{cc: cc for cc in "HK ID IN KH LK MY PH SG TH TW VN".split()}}
        expected = {cc: label for label, ccs in app.items() for cc in ccs.split()}
        self.assertEqual(labels, expected)


if __name__ == "__main__":
    unittest.main()
