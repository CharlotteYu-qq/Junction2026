# Valio Aimo mock API: notes

What we learned from the API docs and by calling the API (13–14 Nov 2026). The portal's ReDoc/Swagger is the source of truth.

## Access

- Base URL `https://junction26.valioaimo.fi`, header `X-API-Key: <key>`. The key is in `.env` and the team chat, never in the repo.
- `GET /api/ping` → `{"status":"ok"}`
- Download endpoints (follow the redirect): `/api/products/download`, `/api/delivery-reliability/download`, `/api/carbon-footprint/download`. Use `python scripts/download_data.py` to fetch them.
- The JSON schema of the product file is only on the portal (it needs the event password).

## On-demand APIs

Both endpoints take `POST {"customer": "...", "requestedDate": "YYYY-MM-DD", "products": ["460013", ...]}` with 1–1,000 product codes.

- **Rate limits** exist but aren't disclosed. Valio asks us to narrow products down with the static data **before** calling, and not to poll the whole catalog.
- **Customer number** doesn't change the answers in the mock, but we pass our customer id anyway.
- **There is no order endpoint.** Placing an order is simulated by the gateway.

### `/api/price`

| Field | Use |
|---|---|
| `catalogPrice.value` | price per **sales unit**, excl. VAT (what we multiply by quantity) |
| `comparisonPrice.value` + `comparisonUnit` (`L`/`KG`) | price per litre/kg. **Trick:** `catalogPrice / comparisonPrice` = content of one sales unit (e.g. 31.31 / 1.57 ≈ 20 L for a case of 2 × 10 L) |
| `tax.taxRate` | 13.5 % for food (MEDIUM), 25.5 % standard |
| `listPrice` | price before the customer discount |
| `netPriceFound` | true if a customer-specific price was used |
| `error` | true if no price exists |

### `/api/availability`

| Field | Meaning / quirk |
|---|---|
| `logisticModel` | `Stock`: Valio Aimo knows the quantity. `Terminal`: delivered from a supplier warehouse, quantity unknown |
| `scheduleLines[0].availableAmount` | in sales units. Stock items often return `9999999999` (plenty); Terminal items always return `999999` (unknown) |
| `scheduleLines[0].availableDate` | earliest date; can be **later** than the requested date |
| `closingTime` | order cut-off for that delivery. **It can already be in the past** (e.g. Thursday 15:00 for a Monday delivery of Terminal items): we treat that as "cut-off passed" |
| `salesBlock`, empty `scheduleLines` | can't be ordered |

Saturday deliveries usually come back with a later `availableDate`.

## Product data (`valio_aimo_junction26_product_data.json`, 87 MB)

- 17,546 products. Every product has `synkkaData`. Names exist in `fi` (all), `en` (17,544) and `sv` (16,100).
- `category` is a code (349 of them) **without names**.
- Unit codes are not translated. Our best guesses: `ST` piece, `KI` case, `PAK` pack, `PRK` tub, `RAS` box, `BOT` bottle, `SK` sack. `units[]` gives `sizeInBaseUnits`, e.g. KI = 5 × ST.
- `allowedLotSize`: order quantity must be a multiple of it (often 0.01).
- Classifications:
  - `allergen` with `CONTAINS` / `MAY_CONTAIN`, and `nonAllergen` with `FREE_FROM`, both using GS1 codes (AM milk, ML lactose, AW gluten cereals, AN tree nuts, AP peanuts, ...).
  - `nutritionalClaim`, e.g. `FREE_FROM_LACTOSE`, `VEGAN`.
  - `packagingMarkedLabel`: certificates such as `EU_ORGANIC_FARMING`, `GOODS_FROM_FINLAND_BLUE_SWAN`, `MARINE_STEWARDSHIP_COUNCIL_LABEL`. It also includes temperature markers.
- `substitutions[]`: Valio Aimo's own substitute rules (189 products).
- Data quality varies a lot: fresh produce often has **no** allergen data at all, and the dataset is synthetic.

## Other files

- `delivery_reliability.csv` (4,425 rows): share of the ordered quantity that was delivered, from 0 to 1. Many products have no value.
- `carbon_footprint.csv` (11,033 rows): kg CO₂e per kg, **an average for the product group**, not a per-product measurement.
