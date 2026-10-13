# GRAD Shopping feed

Builds GRAD's Google Shopping feeds from Shopify and publishes them on GitHub
Pages for Merchant Center to fetch. It replaces the product sync of Shopify's
Google & YouTube app, which sends a country's own domain once that country
moves to one, while Merchant Center can claim only one website.

**Status:** published in **test mode**. Offer IDs and feed labels carry a
`TEST_` prefix, and every item is excluded from every destination, so nothing
can show anywhere.

Feeds and the run report: https://grad-aps.github.io/grad-feed/

## What a run does

1. Reads the active products from the Shopify Storefront API (tokenless, no
   secrets) once per country and language in `config.toml`. That gives the
   translated title and description, the price and currency that country pays,
   and whether the variant can be bought. Shopify's `availableForSale` covers
   stock above 0, selling at 0 stock (policy CONTINUE), and untracked stock.
2. Writes one XML file per **feed label + language**, which is one Merchant
   Center data source. A label shared by several countries is one file, with a
   shipping line per country.
3. Writes `report.md`, `report.json` and `index.html`. With `--check-links`
   it opens one landing page per feed and checks that the domain, path,
   currency and price match the feed.

Every item gets id, title, description (any sentence with alcohol wording
dropped), link, images, availability, price and sale_price, brand, mpn, gtin
when Shopify has a barcode, condition, `google_product_category` 4539 (Wine
Fridges), and shipping per country.

## Links

Set per country in `config.toml` (`link`):

| Countries | Link |
|---|---|
| Euro countries | `{base_url}{folder}/products/{handle}?variant=…`, no `?country=`, standard EUR price |
| Switzerland, USA | `/en-ch/…`, `/en-us/…` |
| Sweden, Canada | today `…&country=SE` / `…&country=CA`; launch `/en-se/…` / `/en-ca/…` |
| Japan, Oceania, Asia, International | main domain with `&country=XX` |

The run stops before publishing if two countries in one feed have different
link rules, or if a euro country's price differs from the standard price.

## Run it

```bash
python feed.py --out site --check-links
```

```bash
python -m unittest -v
```

Exit 0 means written. Exit 2 means written, but with errors in the report.
Exit 1 means nothing was written, so the last good feeds stay live.

GitHub Actions builds the feeds every 6 hours, on every push to
`config.toml` or `feed.py`, and on demand under Actions > Shopping feed > Run
workflow. Only the on-demand run checks links.

## Launch

1. Replace `config.toml` with the launch config. That sets `base_url` to
   gradnordic.com, turns test mode off, and moves Sweden and Canada onto
   their own folders.
2. Push, then run the workflow with link checks.
3. Merchant Center: feed labels can't be changed on an existing data source,
   so create new data sources for the non-TEST files (real labels), and remove
   the TEST ones.
4. Same day, turn off the Google & YouTube app's product sync.
