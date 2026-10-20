"""Regenerate catalog.json: placeholder brands, products and generated prices, the same on every run.

    python evals/ui-tasks/beauty-store/fixtures/generate.py
"""
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent

CATEGORIES = [
    {"id": "100", "name": "Skincare", "children": [
        {"id": "110", "name": "Toners", "children": [{"id": "111", "name": "Mists"}, {"id": "112", "name": "Pads"}]},
        {"id": "120", "name": "Serums", "children": [{"id": "121", "name": "Ampoules"}]},
        {"id": "130", "name": "Cleansers"},
    ]},
    {"id": "200", "name": "Makeup", "children": [
        {"id": "210", "name": "Lips", "children": [{"id": "211", "name": "Tints"}]},
        {"id": "220", "name": "Face"},
    ]},
    {"id": "300", "name": "Hair & Body", "children": [{"id": "310", "name": "Shampoo"}, {"id": "320", "name": "Body Lotion"}]},
    {"id": "400", "name": "Tools"},
]

# Leaf categories and how many products each holds. Skincare totals 52 so its listing has three pages of 24.
LEAVES = {"111": 14, "112": 10, "121": 12, "130": 16, "211": 9, "220": 7, "310": 6, "320": 5}
WORDS = ["Hydrating", "Calming", "Brightening", "Daily", "Mild", "Barrier", "Glow", "Deep", "Soft", "Clear"]
KINDS = {"111": "Toner Mist", "112": "Toner Pad", "121": "Ampoule", "130": "Cleansing Foam", "211": "Lip Tint",
         "220": "Cushion", "310": "Shampoo", "320": "Body Lotion"}


def product_name(index: int) -> str:
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return f"Product {letters}"


def main() -> None:
    rng = random.Random(20261020)
    products = []
    index = 0
    for leaf, count in LEAVES.items():
        for _ in range(count):
            original = round(rng.choice([12, 15, 18, 22, 24, 28, 32, 36, 42]) + rng.choice([0, 0.5, 0.99]), 2)
            discount = rng.choice([0, 0, 10, 15, 20, 30])
            sale = round(original * (100 - discount) / 100, 2)
            product = {
                "id": f"P{index + 1:03d}",
                "name": f"{product_name(index)} {rng.choice(WORDS)} {KINDS[leaf]}",
                "brand": f"Brand {rng.randint(1, 8)}",
                "categoryId": leaf,
                "originalPrice": original,
                "salePrice": sale,
                "popularity": rng.randint(1, 1000),
                "rating": round(rng.uniform(3.6, 5.0), 1),
                "reviewCount": rng.randint(0, 900),
                "options": [],
            }
            if index % 4 == 0:
                sizes = ["50ml", "100ml", "200ml"][: rng.choice([2, 3])]
                options = []
                for position, size in enumerate(sizes):
                    o_original = round(original + 6 * position, 2)
                    o_sale = round(o_original * (100 - discount) / 100, 2)
                    options.append({"id": f"{product['id']}-{position + 1}", "name": size, "originalPrice": o_original,
                                    "salePrice": o_sale, "stock": 50})
                product["options"] = options
            products.append(product)
            index += 1
    # Fixed cases the acceptance checks rely on, independent of the random draw above.
    special = products[0]
    special.update({"id": "P001", "name": "Product A Hydrating Toner Mist", "brand": "Brand 1", "originalPrice": 20.0,
                    "salePrice": 16.0, "options": [
                        {"id": "P001-1", "name": "100ml", "originalPrice": 20.0, "salePrice": 16.0, "stock": 50},
                        {"id": "P001-2", "name": "200ml", "originalPrice": 30.0, "salePrice": 24.0, "stock": 3},
                        {"id": "P001-3", "name": "Refill set", "originalPrice": 34.0, "salePrice": 27.2, "stock": 0}]})
    plain = products[1]
    plain.update({"name": "Product B Calming Toner Mist", "brand": "Brand 2", "originalPrice": 18.0, "salePrice": 18.0,
                  "options": []})
    discounted = products[2]
    discounted.update({"name": "Product C Daily Toner Mist", "brand": "Brand 3", "originalPrice": 30.0, "salePrice": 21.0,
                       "options": []})
    catalog = {
        "version": 1,
        "observed": "2026-10-20",
        "store": "Store",
        "constants": {"freeShippingThreshold": 60.0, "flatShipping": 25.0, "maxQty": 10, "pageSizes": [24, 36, 48]},
        "categories": CATEGORIES,
        "products": products,
        "popularSearches": ["toner", "sunscreen", "serum", "cleanser", "lip tint", "cushion", "ampoule", "pad", "mist",
                            "shampoo"],
        "hero": [{"id": f"H{i}", "title": title, "color": color} for i, (title, color) in enumerate(
            [("New season skincare", "#d8e6d0"), ("Lip colour week", "#f2d4d7"), ("Hair care picks", "#d6e1f0"),
             ("Free shipping over $60", "#f4ead2")], start=1)],
        "rails": [
            {"id": "best-sellers", "title": "Best Sellers", "ranked": True,
             "productIds": [p["id"] for p in sorted(products, key=lambda p: -p["popularity"])[:12]]},
            {"id": "new-arrivals", "title": "New Arrivals", "ranked": False,
             "productIds": [p["id"] for p in products[-10:]]},
        ],
        "locations": [
            {"id": "GLOBAL", "name": "Global (Select Location)", "shippable": False, "currency": None},
            {"id": "US", "name": "United States", "shippable": True, "currency": "USD"},
            {"id": "CA", "name": "Canada", "shippable": True, "currency": "CAD"},
            {"id": "AU", "name": "Australia", "shippable": True, "currency": "AUD"},
            {"id": "JP", "name": "Japan", "shippable": True, "currency": "JPY"},
        ],
        "currencies": ["USD", "CAD", "AUD", "JPY", "EUR"],
    }
    (HERE / "catalog.json").write_text(json.dumps(catalog, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
