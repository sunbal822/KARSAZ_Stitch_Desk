"""Day 1, Test 1: photo + customer message -> structured JSON.
The model only IDENTIFIES the item and PARSES the request. No numbers guessed.
Usage: python extract.py photos/romper.jpg "Want this for a 1 year old in mint green"
"""
import json, sys
import ollama

MODEL = "gemma3:4b"  # try a bigger/newer multimodal Gemma if your machine can run it

with open("catalog.json") as f:
    CATALOG = json.load(f)

PROMPT = """You help a crochet seller process custom orders.
Look at the reference photo and read the customer's request.

Catalog item ids (pick the one that matches the photo, or "unknown"):
{catalog}

Return ONLY JSON with these keys:
- item_ids: catalog ids visible in the photo that the request could refer to
- requested_colors: colors the customer wants (empty if unchanged)
- quantity: number of pieces requested, or null
- requested_size: size, age or measurements the customer said, or null (never put quantity here)
- other_changes: list of any other changes requested
- missing_info: list of 2-4 questions the seller must ask before quoting. Only ask questions that make sense for that kind of item (ages and body measurements only for wearable items). If several items are visible and the request doesn't say which, the first question must ask which item.
Never ask about yarn, material or embellishments. Ask only about size or measurements, deadline, and anything in the request that is unclear.
Do not estimate prices, weights or measurements.

Customer request: {req}"""


def extract(photo, req):
    catalog_desc = {k: v["name"] for k, v in CATALOG.items() if "name" in v}
    r = ollama.chat(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": PROMPT.format(catalog=json.dumps(catalog_desc), req=req),
            "images": [photo],
        }],
        format="json",
        options={"temperature": 0},
    )
    return json.loads(r["message"]["content"])


if __name__ == "__main__":
    print(json.dumps(extract(sys.argv[1], sys.argv[2]), indent=2))