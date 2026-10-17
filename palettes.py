"""Suggested colour palettes, by kind of item. Edit freely: this is the only file you need to touch.

Each palette = (name, one-line mood, [(hex, colour name), ...])   2 or 3 colours.
The key is the "category" from catalog.json (flower, bouquet, baby clothes, plushie, keychain, ...).
Anything not listed uses "general". The first colour is usually the main part (bloom, body), the others the accents.
"""
from colors import TABLE

PALETTES = {
    "flower": [
        ("Classic rose", "Red bloom, deep green leaves", [("#C8283C", "red"), ("#2F6B3A", "dark green")]),
        ("Lilac & sage", "Soft, dreamy and calm", [("#C8A2C8", "lilac"), ("#A3B59A", "sage green")]),
        ("Pink & fresh green", "Sweet spring bouquet", [("#F4A6C0", "pink"), ("#A8D8A0", "light green")]),
        ("Sunshine", "Warm yellow, olive leaves", [("#F7D54A", "yellow"), ("#6E8B3D", "olive green")]),
        ("Peach garden", "Peach bloom, fern leaves", [("#FFCBA4", "peach"), ("#4F7942", "fern green")]),
        ("Blush & ivory", "Pale, wedding-soft", [("#F4EEDB", "ivory"), ("#A3B59A", "sage green")]),
    ],
    "bouquet": [
        ("Classic rose", "Red blooms, deep green leaves", [("#C8283C", "red"), ("#2F6B3A", "dark green")]),
        ("Rose & blush", "Three soft pinks and fern", [("#E8758F", "rose"), ("#F6C6D5", "baby pink"), ("#4F7942", "fern green")]),
        ("Lilac & sage", "Soft, dreamy and calm", [("#C8A2C8", "lilac"), ("#A3B59A", "sage green")]),
        ("Pink & fresh green", "Sweet spring bouquet", [("#F4A6C0", "pink"), ("#A8D8A0", "light green")]),
        ("Sunshine", "Warm yellow, olive leaves", [("#F7D54A", "yellow"), ("#6E8B3D", "olive green")]),
        ("Peach garden", "Peach blooms, fern leaves", [("#FFCBA4", "peach"), ("#4F7942", "fern green")]),
    ],
    "baby clothes": [
        ("Soft pastel", "Gentle pink, cream and mint", [("#F6C6D5", "baby pink"), ("#F5ECD7", "cream"), ("#9ED9B8", "mint")]),
        ("Sunny morning", "Butter yellow and sky", [("#F7E08A", "butter yellow"), ("#F5ECD7", "cream"), ("#AED2F0", "baby blue")]),
        ("Garden calm", "Soft, earthy, nostalgic", [("#E6D8C3", "sand"), ("#A3B59A", "sage green"), ("#C98B8B", "dusty rose")]),
        ("Sky & cloud", "Airy blues and white", [("#AED2F0", "baby blue"), ("#F5F5F5", "white"), ("#F5ECD7", "cream")]),
        ("Lavender dream", "Calm lilac and cream", [("#C9B5E8", "lavender"), ("#F5ECD7", "cream"), ("#F6C6D5", "baby pink")]),
        ("Quiet neutrals", "Warm, timeless, understated", [("#E6DDCB", "oatmeal"), ("#BBAA94", "taupe"), ("#7A6B5E", "mocha")]),
    ],
    "plushie": [
        ("Bubblegum", "Pink, white and hot pink", [("#F4A6C0", "pink"), ("#F5F5F5", "white"), ("#FF69B4", "hot pink")]),
        ("Cocoa", "Cosy browns with a peach tummy", [("#7A5230", "brown"), ("#FFCBA4", "peach"), ("#F5ECD7", "cream")]),
        ("Midnight", "Navy, grey and a mustard pop", [("#1F2F5C", "navy"), ("#9A9A9A", "grey"), ("#D9A520", "mustard")]),
        ("Mint & cream", "Fresh and gentle", [("#9ED9B8", "mint"), ("#F5ECD7", "cream"), ("#F6C6D5", "baby pink")]),
        ("Lilac cloud", "Dreamy purples", [("#C9B5E8", "lavender"), ("#F5F5F5", "white"), ("#8A5BB5", "purple")]),
    ],
    "general": [
        ("Garden calm", "Soft, earthy, nostalgic", [("#E6D8C3", "sand"), ("#A3B59A", "sage green"), ("#C98B8B", "dusty rose")]),
        ("Coastal morning", "Airy blues, sandy neutrals", [("#DCCBAE", "wheat"), ("#9DB4C0", "mist blue"), ("#5F7A8C", "slate blue")]),
        ("Autumn gathering", "Toasty, rich and golden", [("#DDC9A3", "wheat gold"), ("#BF7B56", "terracotta"), ("#7A7550", "moss")]),
        ("Quiet neutrals", "Warm, timeless, understated", [("#E6DDCB", "oatmeal"), ("#BBAA94", "taupe"), ("#7A6B5E", "mocha")]),
        ("Berry & cream", "Deep berry with soft cream", [("#8E2F4F", "berry"), ("#F5ECD7", "cream"), ("#E8758F", "rose")]),
        ("Spring candy", "Pink, mint and butter", [("#F4A6C0", "pink"), ("#9ED9B8", "mint"), ("#F7E08A", "butter yellow")]),
    ],
}
# keychains, cases, bags and accessories share the general set
for _k in ("keychain", "cases", "bag", "accessories"):
    PALETTES.setdefault(_k, PALETTES["general"])

# make every palette colour name understood by the quote and the reply draft
for _lst in PALETTES.values():
    for _n, _t, _cols in _lst:
        for _hex, _name in _cols:
            TABLE.setdefault(_name, _hex)


def get_palettes(category, requested=None, limit=6):
    """Palettes for this kind of item. `requested` = [(hex, name), ...] the customer asked for; shown first."""
    out = []
    if requested:
        out.append({"name": "Customer's pick", "tagline": "From their message", "colors": list(requested)[:3]})
    for name, tagline, cols in PALETTES.get(category, PALETTES["general"]):
        out.append({"name": name, "tagline": tagline, "colors": list(cols)})
    return out[:limit]