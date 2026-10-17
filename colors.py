"""Color name -> hex for the recolor preview. Add your own names to TABLE."""
TABLE = {
    "mint": "#9ED9B8", "mint green": "#9ED9B8", "pink": "#F4A6C0", "baby pink": "#F6C6D5",
    "light pink": "#F8C8DC", "hot pink": "#FF69B4", "rose": "#E8758F", "red": "#C8283C",
    "maroon": "#7B1E2B", "orange": "#F28C38", "peach": "#FFCBA4", "coral": "#F27D72",
    "yellow": "#F7D54A", "mustard": "#D9A520", "gold": "#D4AF37", "cream": "#F5ECD7",
    "ivory": "#F4EEDB", "beige": "#DCC9A8", "white": "#F5F5F5", "black": "#2B2B2B",
    "grey": "#9A9A9A", "gray": "#9A9A9A", "brown": "#7A5230", "skin": "#E8C5A5",
    "green": "#5DAA6B", "light green": "#A8D8A0", "sage": "#A3B59A", "olive": "#808B4A",
    "teal": "#2E8B8B", "blue": "#4A7FC1", "baby blue": "#AED2F0", "sky blue": "#87CEEB",
    "navy": "#1F2F5C", "purple": "#8A5BB5", "lavender": "#C9B5E8", "lilac": "#C8A2C8",
}

def name_to_hex(name):
    """Returns '#RRGGBB' or None if the color is not understood."""
    n = " ".join(str(name).lower().strip().split())
    if n.startswith("#") and len(n) == 7:
        return n.upper()
    if n in TABLE:
        return TABLE[n]
    try:
        from PIL import ImageColor
        r, g, b = ImageColor.getrgb(n.replace(" ", ""))[:3]
        return "#%02X%02X%02X" % (r, g, b)
    except Exception:
        return None


PALETTE_NAMES = ["mint", "baby pink", "pink", "hot pink", "red", "maroon", "coral", "peach", "orange", "mustard",
                 "yellow", "gold", "cream", "white", "beige", "brown", "black", "grey", "sage", "green",
                 "olive", "teal", "sky blue", "baby blue", "blue", "navy", "lavender", "lilac", "purple", "skin"]
PALETTE = [(n, TABLE[n]) for n in PALETTE_NAMES]


def to_hex(v):
    """Accepts '#rrggbb', 'rgb(1,2,3)' or 'rgba(...)' (color pickers differ) -> '#RRGGBB' or None."""
    import re
    if not v: return None
    v = str(v).strip()
    if v.startswith("#") and len(v) >= 7: return v[:7].upper()
    m = re.findall(r"[\d.]+", v)
    if v.lower().startswith("rgb") and len(m) >= 3:
        return "#%02X%02X%02X" % tuple(int(float(x)) for x in m[:3])
    return None

# ---------- colours the customer actually wrote ----------
import re as _re

_BLOOM = {"flower", "flowers", "petal", "petals", "bloom", "blooms", "blossom", "tulip", "tulips", "lily", "sunflower", "daisy", "head"}
_LEAF = {"leaf", "leafs", "leaves", "leave", "stem", "stems", "stalk", "foliage"}


def parse_message_colours(text):
    """Reads the colours the customer wrote. Nothing is guessed: no message -> no colours.
    'red flower and green leaves' -> [{'name': 'red', 'hex': '#C8283C', 'role': 'bloom'}, {'name': 'green', ..., 'role': 'leaf'}]
    role = 'bloom' / 'leaf' when the clause names that part, otherwise None (then the order of mention is used)."""
    if not text or not str(text).strip():
        return []
    t = " " + _re.sub(r"\s+", " ", str(text).lower()) + " "
    out = []
    for clause in _re.split(r"[,;.\n&]| and | with | but | while | then | also ", t):
        words = _re.findall(r"[a-z#0-9]+", clause)
        hits, keys, i = [], [], 0          # hits = (word index, name, hex); keys = (word index, role)
        for j, w in enumerate(words):
            if w in _BLOOM: keys.append((j, "bloom"))
            if w in _LEAF: keys.append((j, "leaf"))
        while i < len(words):
            for n in (3, 2, 1):
                cand = " ".join(words[i:i + n])
                if len(words[i:i + n]) < n:
                    continue
                if n > 1 and cand not in TABLE:
                    continue
                if n == 1 and not (cand in TABLE or (len(cand) >= 3 and name_to_hex(cand))):
                    continue
                hits.append((i, cand, name_to_hex(cand)))
                i += n
                break
            else:
                i += 1
        for idx, name, hx in hits:
            role = None
            if keys:
                best = min(keys, key=lambda k: abs(k[0] - idx))
                dists = sorted(abs(k[0] - idx) for k in keys)
                if len(dists) == 1 or dists[0] != dists[1] or len({k[1] for k in keys}) == 1:
                    role = best[1]
            if not any(o["name"] == name and o["role"] == role for o in out):
                out.append({"name": name, "hex": hx, "role": role})
    return out