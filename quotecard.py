"""Quote card: one clean picture with before/after, price and ready date, ready to send to a customer."""
import os, textwrap
from PIL import Image, ImageDraw, ImageFont, ImageOps
from quote import HERE

CREAM, INK, ROSE, SOFT, LINE = "#fffbf7", "#4a0a0a", "#c4999a", "#fdf1ee", "#ecd0cb"
W, H = 1080, 1500


def _font(size, bold=False):
    names = (["card_font_bold.ttf", "card_font.ttf"] if bold else ["card_font.ttf"])
    for n in names:                                   # optional: drop your own font into assets/
        p = os.path.join(HERE, "assets", n)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    for p in ("DejaVuSerif-Bold.ttf", "DejaVuSans-Bold.ttf") if bold else ("DejaVuSerif.ttf", "DejaVuSans.ttf"):
        for d in ("/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/", "/Library/Fonts/", ""):
            try:
                return ImageFont.truetype(d + p if d else p, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _center(d, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def _frame(card, path, box, label):
    x, y, w, h = box
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((x, y, x + w, y + h), 28, fill="white", outline=LINE, width=3)
    try:
        im = ImageOps.contain(Image.open(path).convert("RGB"), (w - 36, h - 36))
        card.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
    except Exception:
        pass
    f = _font(30, True)
    tw = d.textlength(label, font=f)
    d.rounded_rectangle((x + (w - tw) / 2 - 22, y - 22, x + (w + tw) / 2 + 22, y + 26), 24, fill=INK)
    d.text((x + (w - tw) / 2, y - 14), label, font=f, fill=CREAM)


def make_card(out_path, shop, logo_path, before, after, item, price_text, details, ready_text, customer="", note=""):
    card = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(card)
    for cx, cy, r in ((70, 60, 120), (W - 70, 60, 120)):          # soft yarn-ball corners
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#f3e1de")
    y = 50
    if logo_path and os.path.exists(logo_path):
        try:
            lg = ImageOps.contain(Image.open(logo_path).convert("RGBA"), (170, 170))
            card.paste(lg, ((W - lg.width) // 2, y), lg)
            y += lg.height + 10
        except Exception:
            pass
    shop_f = _font(52, True)
    _center(d, y, shop, shop_f, INK)
    y += 84
    if customer:
        _center(d, y, f"Quote for {customer}", _font(32), "#7a5352")
        y += 52
    y += 24
    if after:
        fw, fh, gap = 470, 520, 40
        x0 = (W - 2 * fw - gap) // 2
        _frame(card, before, (x0, y + 30, fw, fh), "BEFORE")
        _frame(card, after, (x0 + fw + gap, y + 30, fw, fh), "AFTER")
    else:
        fw, fh = 620, 520
        _frame(card, before, ((W - fw) // 2, y + 30, fw, fh), "YOUR ITEM")
    y += 30 + 520 + 50
    box = (70, y, W - 70, y + 400)
    d.rounded_rectangle(box, 34, fill=SOFT, outline=LINE, width=3)
    _center(d, y + 28, item, _font(46, True), INK)
    _center(d, y + 98, price_text, _font(110, True), "#5c0a0a")
    ty = y + 236
    for line in details[:2]:
        _center(d, ty, textwrap.shorten(line, 50, placeholder="..."), _font(32), "#6b3c3a")
        ty += 46
    _center(d, y + 400 - 66, ready_text, _font(36, True), INK)
    _center(d, H - 58, note or "Colours on screen may look slightly different from the real yarn.", _font(25), "#9a6f6c")
    card.save(out_path)
    return out_path