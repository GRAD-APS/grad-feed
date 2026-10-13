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

    def test_euro_countries_use_the_language_folder_and_no_country(self):
        self.assertEqual(self.link(CFG, "FR", "fr"),
                         "https://grad.dk/fr/products/grad-no-1-steel-series?variant=47061726298450")
        self.assertEqual(self.link(CFG, "DE", "en"),
                         "https://grad.dk/products/grad-no-1-steel-series?variant=47061726298450")
        self.assertEqual(self.link(CFG, "DK", "da"),
                         "https://grad.dk/da/products/grad-no-1-steel-series?variant=47061726298450")

    def test_market_subfolders(self):
        self.assertEqual(self.link(CFG, "CH", "en"),
                         "https://grad.dk/en-ch/products/grad-no-1-steel-series?variant=47061726298450")
        self.assertEqual(self.link(CFG, "US", "en"),
                         "https://grad.dk/en-us/products/grad-no-1-steel-series?variant=47061726298450")

    def test_main_domain_countries_keep_country(self):
        self.assertEqual(self.link(CFG, "SE", "en"),
                         "https://grad.dk/products/grad-no-1-steel-series?variant=47061726298450&country=SE")
        self.assertTrue(self.link(CFG, "JP", "en").endswith("&country=JP"))

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


class ConfigTest(unittest.TestCase):
    def test_uk_is_out(self):
        self.assertNotIn("GB", CFG["countries"])

    def test_live_pmax_label_is_kept(self):
        self.assertEqual(CFG["countries"]["FR"]["feed_label"], "EUR_62471242066")

    def test_links_never_use_a_country_domain(self):
        self.assertNotRegex(CFG["base_url"], r"gradnordic\.(?!com)")

    def test_launch_config_differs_only_where_intended(self):
        launch_path = HERE / "config.launch.toml"
        if not launch_path.exists():
            self.skipTest("config.launch.toml is kept local")
        launch = feed.load_config(launch_path)
        self.assertEqual(launch["base_url"], "https://gradnordic.com")
        self.assertFalse(launch["test_mode"])
        self.assertEqual(feed.link_pattern(launch, "SE"), "{base_url}/en-se/products/{handle}?variant={variant}")
        self.assertEqual(feed.link_pattern(launch, "CA"), "{base_url}/en-ca/products/{handle}?variant={variant}")
        test = tomllib.loads((HERE / "config.toml").read_text(encoding="utf-8"))
        raw = tomllib.loads(launch_path.read_text(encoding="utf-8"))
        for cc in set(test["countries"]) - {"SE", "CA"}:
            self.assertEqual(raw["countries"][cc], test["countries"][cc], cc)


if __name__ == "__main__":
    unittest.main()
