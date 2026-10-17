"""End to end: photo + customer message -> extract -> preview -> quote -> reply draft -> (optional) log.
Usage: python pipeline.py photos\\romper.jpeg "Want this for a 1 year old in mint green" --customer Ayesha [--due 2026-11-20] [--log]
The model only identifies the item and parses the request; every number comes from quote.py.
"""
import argparse, datetime, json, os, re, time
from quote import quote, log_order, due_date, CAT, HERE
from colors import name_to_hex


def parse_age_months(text):
    """'1 year old' -> 12, '18 months' -> 18, '6-12 months' -> 9, 'newborn' -> 0. None if unclear."""
    if not text: return None
    t = str(text).lower()
    if "newborn" in t or "new born" in t: return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(month|mo|year|yr)", t)
    if m:
        mid = (float(m.group(1)) + float(m.group(2))) / 2
        return mid * (12 if m.group(3).startswith(("year", "yr")) else 1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(month|mo|year|yr)", t)
    if m: return float(m.group(1)) * (12 if m.group(2).startswith(("year", "yr")) else 1)
    return None


def resolve_ids(raw):
    """Accepts 'star_cocoon', 'Star cocoon', 'star-cocoon', 'Star baby cocoon / swaddle'... -> catalog ids (no duplicates)."""
    if isinstance(raw, str): raw = [raw]
    norm = lambda t: re.sub(r"[^a-z0-9]+", "_", str(t).lower()).strip("_")
    names = {norm(v["name"]): k for k, v in CAT.items() if "name" in v}
    found = []
    for r in raw or []:
        n = norm(r)
        k = n if n in CAT else names.get(n)
        if k is None:  # e.g. 'star_cocoon_swaddle' contains id 'star_cocoon'
            hits = [cid for cid in CAT if len(cid) > 3 and (cid in n or n in cid) and n]
            k = hits[0] if len(hits) == 1 else None
        if k and k not in found: found.append(k)
    return found


def as_list(x):
    """The model sometimes returns text instead of a list. None/'' -> [], 'mint green' -> ['mint green']."""
    if x is None: return []
    if isinstance(x, (list, tuple)): return [str(i).strip() for i in x if str(i).strip()]
    return [p.strip() for p in re.split(r"[,;\n]", str(x)) if p.strip()]


def as_int(x, default=1):
    try: return max(int(float(x)), 1)
    except (TypeError, ValueError): return default


def clean_questions(qs, age, due):
    """Drop duplicates and questions we can already answer (size chart covers measurements; deadline is a field)."""
    out = []
    for q in qs:
        low = q.lower()
        if age is not None and any(w in low for w in ("length", "measure", "height", "chest", "current size")): continue
        if due and any(w in low for w in ("deadline", "by when", "needed by")): continue
        if q not in out: out.append(q)
    return out


def process(ex, customer="", log=False, due=None, cfg=None):
    """ex = JSON dict from extract(). Returns a dict: quote, questions, hex, due, ..."""
    ex = dict(ex)
    for k in ("requested_colors", "other_changes", "missing_info"):
        ex[k] = as_list(ex.get(k))
    res = {"quote": None, "questions": list(ex["missing_info"]), "hex": None,
           "color_name": None, "due": None, "due_warning": None, "logged": False}
    ids = resolve_ids(ex.get("item_ids"))
    if len(ids) != 1:
        res["questions"].insert(0, "Which item do you want?")
        return res
    item = ids[0]
    wearable = CAT[item].get("base_age_months") is not None
    age = parse_age_months(ex.get("requested_size")) if wearable else None
    if wearable and ex.get("requested_size") and age is None:
        res["questions"].insert(0, f"Could not read the size '{ex['requested_size']}'. What age in months?")

    req_colors = ex["requested_colors"]
    for c in req_colors:
        h = name_to_hex(c)
        if h and not res["hex"]:
            res["hex"], res["color_name"] = h, c
        elif not h:
            res["questions"].insert(0, f"Which exact shade is '{c}'? A photo or hex code would help.")
    colors = max(CAT[item]["colors"], len(req_colors))
    changes = ex["other_changes"]

    q = quote(item, colors=colors, age=age, qty=as_int(ex.get("quantity")), n_changes=len(changes), cfg=cfg)
    res["quote"] = q
    earliest = due_date(q["hours"] * q["qty"], cfg=cfg)
    res["earliest"] = earliest
    res["due"] = earliest
    if due:
        try:
            wanted = datetime.date.fromisoformat(str(due))
            res["due"] = wanted
            if wanted < earliest:
                res["due_warning"] = f"Customer wants it by {wanted}, but at your pace the earliest is {earliest}."
        except ValueError:
            res["questions"].insert(0, f"Could not read the date '{due}'. Use YYYY-MM-DD.")
    res["questions"] = clean_questions(res["questions"], age, due)
    res["req_colors"], res["changes"] = req_colors, changes
    if log:
        log_order(q, customer, ", ".join(req_colors), "; ".join(map(str, changes)), res["due"], cfg=cfg)
        res["logged"] = True
    return res


def log_result(res, customer="", path=None, cfg=None):
    """Writes a finished quote (from process()) to orders.xlsx."""
    log_order(res["quote"], customer, ", ".join(res["req_colors"]), "; ".join(map(str, res["changes"])), res["due"],
              notes=("Yarn by colour: " + res["yarn_text"]) if res.get("yarn_text") else "", path=path, cfg=cfg)


def make_preview(photo, hex_color, out_dir=None):
    """Recolors the photo with recolor_v3.py. Returns (path or None, note)."""
    out_dir = out_dir or os.path.join(HERE, "previews")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"preview_{int(time.time())}.png")
    try:
        from recolor_v3 import recolor
    except ImportError as e:
        return None, f"Preview unavailable ({e})."
    try:
        recolor(photo, hex_color, out)
    except SystemExit as e:
        return None, str(e)
    except Exception as e:
        return None, f"Preview failed: {e}"
    return out, "Preview recolors the main color only; the real yarn may look slightly different on screen."


def _age_text(m):
    if m is None: return None
    if m == 0: return "a newborn"
    if m % 12 == 0:
        y = int(m // 12)
        return f"{y} year" if y == 1 else f"{y} years"
    return f"{m:g} months"


def colour_text(res):
    names = [("the colour shown in the preview" if str(c).startswith("#") else str(c)) for c in (res.get("req_colors") or [])]
    names = list(dict.fromkeys(names))
    return (" in " + " and ".join(names)) if names else ""


def make_reply(res, customer=""):
    """Plain template, no model: numbers cannot be invented."""
    q = res["quote"]
    if not q:
        return "Thank you for your message! Which item from the photo would you like me to make?"
    lines = [f"Hi {customer or 'there'}! Thank you for your order.", "",
             f"Here's what I understood: {q['item']}" + colour_text(res) + "."]
    if q["size_to"] is not None:
        lines.append(f"Size: for {_age_text(q['size_to'])}" +
                     (f" (about {q['size_cm']['chest_cm']} cm chest, {q['size_cm']['height_cm']} cm height)." if q["size_cm"] else "."))
    price = f"Price: Rs {q['total_price']:,}"
    if q["qty"] > 1: price += f" ({q['qty']} x Rs {q['unit_price']:,})"
    lines.append(price + ".")
    ready = res["earliest"] if res["due_warning"] else res["due"]  # never promise a date you can't meet
    lines.append(f"Ready by about {ready:%d %b %Y}.")
    if res.get("has_preview"):
        lines.append("I've attached a preview of the color. The real yarn may look slightly different on your screen.")
    if res["questions"]:
        lines += ["", "Before I confirm, could you tell me:"] + [f"- {x}" for x in res["questions"]]
    return "\n".join(lines)


def run(photo, request, customer="", log=False, due=None, preview=True, extractor=None, item_override=None):
    if extractor is None:
        from extract import extract as extractor  # needs ollama running
    ex = extractor(photo, request)
    if item_override:  # the seller picked the item by hand
        ex = dict(ex, item_ids=[item_override])
    res = process(ex, customer, log, due)
    res["extracted"] = ex
    res["preview_path"], res["preview_note"] = None, ""
    if preview and res["quote"] and res["hex"]:
        res["preview_path"], res["preview_note"] = make_preview(photo, res["hex"])
    res["has_preview"] = bool(res["preview_path"])
    res["reply"] = make_reply(res, customer)
    return res


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("photo"); p.add_argument("request")
    p.add_argument("--customer", default=""); p.add_argument("--due"); p.add_argument("--log", action="store_true")
    p.add_argument("--no-preview", action="store_true")
    a = p.parse_args()
    r = run(a.photo, a.request, a.customer, a.log, a.due, not a.no_preview)
    print("EXTRACTED:", json.dumps(r["extracted"], indent=2))
    print("\nQUOTE:", json.dumps(r["quote"], indent=2, default=str))
    if r["preview_path"]: print("\nPREVIEW:", r["preview_path"])
    elif r["preview_note"]: print("\nPREVIEW:", r["preview_note"])
    if r["due_warning"]: print("\nWARNING:", r["due_warning"])
    if r["logged"]: print("\nLogged to orders.xlsx")
    print("\nREPLY DRAFT:\n" + r["reply"])