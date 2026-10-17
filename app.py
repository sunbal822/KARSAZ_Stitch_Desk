"""Crochet order assistant, step by step.  Run: python app.py
1 photo -> 2 colour change? -> 3 click each part and pick its colour -> 4 size, deadline, details -> 5 before/after + reply draft
"""
import os, time
import gradio as gr
from PIL import Image
from colors import PALETTE, name_to_hex, to_hex, parse_message_colours
from pipeline import process, make_reply, log_result, resolve_ids, as_list, as_int
from quote import CAT, HERE
from palettes import get_palettes
import datetime, math, uuid
import accounts, quotecard
import orders as ordersmod
from quote import CFG as DEFAULT_CFG
from pipeline import colour_text, _age_text

CHOICES = [("Not sure yet", "")] + [(v["name"], k) for k, v in CAT.items() if "name" in v]
OUT_DIR = os.path.join(HERE, "previews")
_RC = {}  # one PartRecolorer per photo, so the photo is analysed only once


def get_rc(path):
    from recolor_v5 import PartRecolorer
    if path not in _RC:
        if len(_RC) >= 2: _RC.pop(next(iter(_RC)))
        _RC[path] = PartRecolorer(path)
    return _RC[path]


def new_state():
    return {"photo": None, "ex": {}, "cust_color": None, "base": None, "current": None, "parts": [],
            "sel": None, "res": None, "customer": "", "logged": False,
            "palettes": [], "active": None, "item_name": "", "cat": "general", "cust": [], "poly": [], "natural": True}


def nav(n):
    return [gr.update(visible=(i == n)) for i in range(5)]


_last_clean = [0.0]


def _cleanup():
    """Delete preview pictures older than 12 hours (checked at most every 10 minutes)."""
    now = time.time()
    if now - _last_clean[0] < 600: return
    _last_clean[0] = now
    for f in os.listdir(OUT_DIR):
        p = os.path.join(OUT_DIR, f)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > 12 * 3600: os.remove(p)
        except OSError:
            pass


def _save(img, prefix):
    os.makedirs(OUT_DIR, exist_ok=True)
    _cleanup()
    path = os.path.join(OUT_DIR, f"{prefix}_{uuid.uuid4().hex[:12]}.png")
    img.save(path)
    return path


def render_current(st):
    rc = get_rc(st["photo"])
    if not st["parts"]:
        st["current"] = st["base"]
    else:
        st["current"] = _save(rc.render(st["parts"], natural=bool(st.get("natural", True))), "preview")
    return st["current"]


def show_selection(st):
    rc = get_rc(st["photo"])
    s = st["sel"]
    base = rc.img if not st["current"] or st["current"] == st["base"] else Image.open(st["current"])
    return _save(rc.overlay(base, s["points"], s["tol"], s["grow"], s["smart"], s.get("rid")), "select")


def _next_hint(st):
    a = st.get("active")
    if a is None or a >= len(st["palettes"]): return ""
    p = st["palettes"][a]
    used = {x["hex"].upper() for x in st["parts"]}
    left = [n for h, n in p["colors"] if h.upper() not in used]
    return (f"\n\nStill to use from **{p['name']}**: " + ", ".join(left)) if left else f"\n\nAll of **{p['name']}** is used."


def status_text(st, extra=""):
    rc = get_rc(st["photo"])
    note = f"\n\n(Smart selection is off, using colour selection instead: {rc.sam_error})" if rc.sam_error else ""
    act = st["palettes"][st["active"]]["name"] if st.get("active") is not None and st["active"] < len(st["palettes"]) else None
    if st["sel"]:
        msg = ("🟡 **Part selected.** The yellow area will change. Missed a bit? Click it. "
               "Took too much? Open *Fine-tune* and choose Remove area. Then tap a colour." + _next_hint(st))
    elif st["parts"]:
        msg = f"✅ **{len(st['parts'])} part(s) coloured.** Click another part, or press **Next**." + _next_hint(st)
    elif act:
        msg = f"🧵 **{act}** picked. Click a part of the photo, then tap its colour."
    else:
        msg = "👆 **Click a part of the photo** to start. The first click can take a minute while the photo is analysed."
    return (extra + "\n\n" if extra else "") + msg + note


# ---------- auto colouring ----------
def _yarn_only(clusters):
    """Flowers and bouquets: leave out wrapper paper / newspaper (pale, low colour, scattered in strips)."""
    yarn = [c for c in clusters if not (c["sat"] < 0.22 and c["solid"] < 0.5)]
    return yarn or clusters


def assign_customer(clusters, cust, cat):
    """Colours the customer wrote in their message. cust = parse_message_colours() output.
    Flowers: 'flower red, leaves green' puts red on the bloom and green on the leaves; a colour with no part named
    fills whichever part has none yet (first one = bloom). Other items: order of mention, biggest part first."""
    if not clusters or not cust:
        return []
    clusters = sorted(clusters, key=lambda c: -c["area"])
    if cat in ("flower", "bouquet"):
        bloom = [(c["hex"], c["name"]) for c in cust if c["role"] == "bloom"]
        leaf = [(c["hex"], c["name"]) for c in cust if c["role"] == "leaf"]
        free = [(c["hex"], c["name"]) for c in cust if c["role"] is None]
        if bloom or leaf:
            if free and not bloom: bloom, free = free[:1], free[1:]
            if free and not leaf: leaf = free[:1]
            clusters = _yarn_only(clusters)
            green = [c for c in clusters if c["green"]]
            other = [c for c in clusters if not c["green"]]
            out = []
            if bloom: out += [(c, *bloom[min(i, len(bloom) - 1)]) for i, c in enumerate(other)]
            if leaf: out += [(c, *leaf[0]) for c in green]
            if out: return out
    cols = []
    for c in cust:
        if (c["hex"], c["name"]) not in cols: cols.append((c["hex"], c["name"]))
    return assign_colours(clusters, cols, cat)


def assign_colours(clusters, cols, cat):
    """Decides which palette colour goes on which part of the item.
    cols = [(hex, name), ...]. Flowers: bloom parts get the first colour(s), green parts get the LAST colour (leaves).
    Everything else: biggest part gets colour 1, next biggest colour 2, and so on."""
    if not clusters or not cols:
        return []
    clusters = sorted(clusters, key=lambda c: -c["area"])
    if cat in ("flower", "bouquet"):
        # wrapper paper / newspaper: pale, low colour, scattered in strips. Never recolour it.
        clusters = _yarn_only(clusters)
        green = [c for c in clusters if c["green"]]
        other = [c for c in clusters if not c["green"]]
        if green and other:
            if len(cols) == 1:                      # one colour asked for: the leaves stay green
                return [(c, *cols[0]) for c in other]
            bloom = cols[:-1]
            return [(c, *bloom[min(i, len(bloom) - 1)]) for i, c in enumerate(other)] + [(c, *cols[-1]) for c in green]
    out = []
    for i, c in enumerate(clusters):
        if i < len(cols): j = i
        elif len(cols) == 1: continue
        else: j = len(cols) - 1
        out.append((c, *cols[j]))
    return out


def auto_colour(st):
    """Colours every part of the item with the active palette. Replaces the current colouring."""
    rc = get_rc(st["photo"])
    pal = st["palettes"][st["active"]]
    if pal.get("customer") and st.get("cust"):
        picks = assign_customer(rc.auto_parts(), st["cust"], st.get("cat", "general"))
    else:
        picks = assign_colours(rc.auto_parts(), pal["colors"], st.get("cat", "general"))
    if not picks:
        raise ValueError("no parts found")
    st["parts"] = [{"points": c["points"], "rid": c["rid"], "hex": h, "name": n, "tol": 1.0, "grow": 4, "smart": True}
                   for c, h, n in picks]
    st["sel"] = None
    return render_current(st)


def _cust_text(cust):
    role = {"bloom": " (flower)", "leaf": " (leaves)"}
    return ", ".join(f"{c['name']}{role.get(c['role'], '')}" for c in cust)


# ---------- step 1 ----------
def start(photo, message, st):
    if not photo:
        return (st, "Please upload a photo first.", *nav(0), "")
    st = new_state()
    st["photo"] = photo
    msg = (message or "").strip()
    ex = {"item_ids": [], "requested_colors": [], "quantity": 1, "requested_size": None,
          "other_changes": [], "missing_info": []}
    note = ""
    try:
        from extract import extract
        ex.update(extract(photo, msg or "Customer wants this item as shown."))
    except Exception as e:
        note = f"(Could not read the photo automatically: {e}. You can fill things in by hand.)\n\n"
    if not msg:   # nothing was written, so nothing can be requested: the model must not invent colours, sizes or changes
        ex.update(requested_colors=[], quantity=1, requested_size=None, other_changes=[])
    # Colours come ONLY from the words the customer wrote (never from the model's guess)
    st["cust"] = parse_message_colours(msg)
    ex["requested_colors"] = [c["name"] for c in st["cust"]]
    st["ex"] = ex
    info = note + (f"Customer asked for: **{_cust_text(st['cust'])}**" if st["cust"] else "No colour mentioned in the message.")
    return (st, "", *nav(1), info)


# ---------- step 2 -> 3 ----------
def _pal_css(st):
    css = ""
    for i, p in enumerate(st["palettes"]):
        cs = [h for h, _ in p["colors"]] + [None] * 3
        for k in range(3):
            css += f"#pal-{i}{{--c{k+1}:{cs[k] or 'transparent'};--g{k+1}:{'rgba(60,40,30,.28)' if cs[k] else 'transparent'};}}"
    a = st.get("active")
    if a is not None and a < len(st["palettes"]):
        css += "#pal-%d{border-color:#c96a4a !important;background:#fdeadc !important;box-shadow:0 0 0 3px rgba(201,106,74,.28) !important;}" % a
        for k, (h, n) in enumerate(st["palettes"][a]["colors"]):
            css += f"#pc-{k}, #pc-{k} button{{background:{h} !important;color:{_text_color(h)} !important;}}"
    return "<style>" + css + "</style>"


def _pal_updates(st):
    """css, 6 palette cards, note, 3 palette-colour chips"""
    ps = st["palettes"]
    cards = [gr.update(value=f"{p['name']}\n{p['tagline']}", visible=True) if i < len(ps) else gr.update(visible=False)
             for i, p in enumerate(ps + [None] * (6 - len(ps)))]
    a = st.get("active")
    cols = ps[a]["colors"] if a is not None and a < len(ps) else []
    chips = [gr.update(value=cols[k][1].title(), visible=True) if k < len(cols) else gr.update(visible=False) for k in range(3)]
    kind = st["item_name"] or "your item"
    note = (f"<div class='sec-title'>Colour palettes</div>"
            f"<div class='hint'>Suggested for <b>{kind}</b>. Tap one and every part is coloured for you. "
            f"To change a single part, click it and tap another colour.</div>")
    return [_pal_css(st), *cards, note, *chips]


def yes(st):
    rc = get_rc(st["photo"])
    os.makedirs(OUT_DIR, exist_ok=True)
    st["base"] = _save(rc.img, "base")
    st["current"], st["parts"], st["sel"], st["active"] = st["base"], [], None, None
    ex = st["ex"]
    ids = resolve_ids(ex.get("item_ids"))
    cat = CAT[ids[0]]["category"] if len(ids) == 1 else "general"
    st["item_name"] = CAT[ids[0]].get("name", "").lower() if len(ids) == 1 else ""
    st["cat"] = cat
    cust = st.get("cust") or []
    # card colours: flower colour(s) first, leaf colour last (same order the palettes use)
    order = [c for c in cust if c["role"] == "bloom"] + [c for c in cust if c["role"] is None] + [c for c in cust if c["role"] == "leaf"]
    req = [(c["hex"], c["name"]) for c in order]
    st["palettes"] = get_palettes(cat, req)      # no "Customer's pick" card when the message named no colour
    note = ""
    if cust:                                      # the customer named colours: apply them right away
        st["palettes"][0]["customer"] = True
        st["active"] = 0
        try:
            auto_colour(st)
        except Exception as e:
            note = f"Could not colour it automatically ({e}). Click a part of the photo and tap its colour instead."
    return (st, *nav(2), st["current"], status_text(st, note), gr.update(visible=bool(st["parts"])), *_pal_updates(st))


# ---------- step 3: click, select, colour ----------
def _shape_view(st, rc, closed=False):
    base = rc.img if not st["current"] or st["current"] == st["base"] else Image.open(st["current"])
    return _save(rc.overlay_poly(base, st["poly"], closed), "shape") if st["poly"] else (st["current"] or st["base"])


def on_click(st, mode, smart, evt: gr.SelectData):
    x, y = evt.index[0], evt.index[1]
    rc = get_rc(st["photo"])
    if mode == "Draw shape":     # click the corners of a part (like a polygon lasso), then press "Close shape"
        if st["sel"] is not None:
            return st, gr.update(), status_text(st, "Finish this part first: tap a colour, or press **Cancel selection**."), gr.update()
        st["poly"].append((x, y))
        return (st, _shape_view(st, rc), status_text(st, f"✏️ **{len(st['poly'])} corner(s).** Click the next corner. "
                "Go a little outside the edge of the item, it is trimmed automatically. When the shape is done press **Close shape**."), gr.update())
    if st["sel"] is not None and st["sel"].get("rid") in rc._exact:
        return st, gr.update(), status_text(st, "A drawn shape can't be extended. Tap a colour, or **Cancel selection** and draw it again."), gr.update(visible=True)
    if st["sel"] is None:
        if mode == "Remove area":
            return st, gr.update(), status_text(st, "Nothing is selected yet. Switch to **Add area** and click the part first."), gr.update()
        i = rc.owner((x, y, 1), st["parts"])
        if i is not None:  # clicked a part that is already recoloured: edit it
            p = st["parts"][i]
            st["sel"] = {"points": list(p["points"]), "edit": i, "tol": p["tol"], "grow": p["grow"], "smart": p.get("smart", True), "rid": p.get("rid")}
        else:
            st["sel"] = {"points": [(x, y, 1)], "edit": None, "tol": 1.0, "grow": 4, "smart": bool(smart), "rid": None}
    else:
        st["sel"]["points"].append((x, y, 1 if mode == "Add area" else 0))
    return st, show_selection(st), status_text(st), gr.update(visible=True)


def close_shape(st):
    rc = get_rc(st["photo"])
    if len(st["poly"]) < 3:
        return st, gr.update(), status_text(st, "Click at least 3 corners first."), gr.update()
    rid = rc.add_polygon(st["poly"])
    sx, sy = rc.region_seed(rid)
    st["poly"] = []
    st["sel"] = {"points": [(sx, sy, 1)], "edit": None, "tol": 1.0, "grow": 4, "smart": False, "rid": rid}
    return st, show_selection(st), status_text(st), gr.update(visible=True)


def undo_corner(st):
    rc = get_rc(st["photo"])
    if st["poly"]: st["poly"].pop()
    return st, _shape_view(st, rc), status_text(st, f"{len(st['poly'])} corner(s) so far." if st["poly"] else "No corners yet."), gr.update()


def mode_changed(mode):
    return gr.update(visible=(mode == "Draw shape"))


def natural_toggle(st, on):
    st["natural"] = bool(on)
    path = render_current(st) if st["parts"] else gr.update()
    return st, path, status_text(st), gr.update()


def apply_colour(st, name, hexv):
    s = st["sel"]
    if not s:
        return st, gr.update(), status_text(st, "Click a part of the item first."), gr.update(visible=False)
    part = {"points": s["points"], "hex": hexv, "name": name, "tol": s["tol"], "grow": s["grow"], "smart": s["smart"], "rid": s.get("rid")}
    if s["edit"] is not None:
        st["parts"][s["edit"]] = part
    else:
        st["parts"].append(part)
    st["sel"] = None
    try:
        path = render_current(st)
    except Exception as e:
        return st, gr.update(), status_text(st, f"Preview failed: {e}"), gr.update(visible=False)
    return st, path, status_text(st), gr.update(visible=False)


def apply_picker(st, value):
    h = to_hex(value)
    if not h:
        return st, gr.update(), status_text(st, "That colour could not be read. Try a palette colour."), gr.update()
    return apply_colour(st, h, h)


def clear_selection(st):
    st["sel"], st["poly"] = None, []
    return st, st["current"] or st["base"], status_text(st), gr.update(visible=False)


def undo(st):
    st["sel"] = None
    if st["parts"]: st["parts"].pop()
    return st, render_current(st), status_text(st), gr.update(visible=False)


def start_over(st):
    st["sel"], st["parts"], st["poly"] = None, [], []
    return st, render_current(st), status_text(st), gr.update(visible=False)


def adjust(st, key):
    t = st["sel"] or (st["parts"][-1] if st["parts"] else None)
    if t is None:
        return st, gr.update(), status_text(st, "Select or recolour a part first."), gr.update()
    if key == "more": t["grow"] = min(t["grow"] + 2, 15)
    elif key == "less": t["grow"] = max(t["grow"] - 2, 1)
    elif key == "loose": t["tol"] = min(t["tol"] * 1.25, 2.0)
    elif key == "strict": t["tol"] = max(t["tol"] / 1.25, 0.65)
    elif key == "reset": t["tol"], t["grow"] = 1.0, 4
    try:
        path = show_selection(st) if st["sel"] else render_current(st)
    except Exception as e:
        return st, gr.update(), status_text(st, f"Preview failed: {e}"), gr.update()
    return st, path, status_text(st, f"Settings: colour match {t['tol']:.2f}, edge cover {t['grow']}"), gr.update()


def pick_palette(st, i):
    """Tapping a palette colours the whole item automatically. Outputs: st, canvas, status, css, 3 chips, picker box."""
    if i >= len(st["palettes"]):
        u = _pal_updates(st)
        return [st, gr.update(), status_text(st), u[0], *u[-3:], gr.update()]
    st["active"] = i
    note, path = "", gr.update()
    try:
        path = auto_colour(st)
    except Exception as e:
        note = f"Auto colouring did not work ({e}). Click a part of the photo and tap its colour instead."
    u = _pal_updates(st)
    return [st, path, status_text(st, note), u[0], *u[-3:], gr.update(visible=True)]


def apply_chip(st, k):
    a = st.get("active")
    if a is None or k >= len(st["palettes"][a]["colors"]):
        return st, gr.update(), status_text(st), gr.update()
    hexv, name = st["palettes"][a]["colors"][k]
    return apply_colour(st, name, hexv)


# ---------- step 4 ----------
def _wearable(item):
    """Only wearables (baby clothes) have an age/size: it scales yarn, hours and price and picks the size-chart row."""
    return bool(item) and CAT.get(item, {}).get("base_age_months") is not None


def _show_size(item):
    return (not item) or _wearable(item)       # item not known yet: keep the field so nothing is lost


def size_visibility(item):
    return gr.update(visible=_show_size(item))


def go_details(st):
    ex = st["ex"]
    ids = resolve_ids(ex.get("item_ids"))
    item = ids[0] if len(ids) == 1 else ""
    msg = "" if item else "I could not tell which item this is. Please pick it."
    size = (ex.get("requested_size") or "") if _show_size(item) else ""
    return (st, *nav(3), item, gr.update(value=size, visible=_show_size(item)), as_int(ex.get("quantity")),
            ", ".join(as_list(ex.get("other_changes"))), msg)


def skip_colour(st):
    st["parts"], st["sel"] = [], None
    return go_details(st)


def next_from_parts(st):
    st["sel"] = None
    return go_details(st)


# ---------- step 5 ----------
def yarn_split(st, q):
    """Grams per colour, from the picture area of each coloured part (flat area is a fair guide to yarn). Estimate only."""
    if not st["parts"]:
        return ""
    try:
        rc = get_rc(st["photo"])
        counts = rc.areas(st["parts"])
        total = sum(counts)
        if st.get("cat") not in ("flower", "bouquet"):          # a bouquet photo includes the wrapper: share among yarn parts only
            total = max(total, rc.item_area())
        if total <= 0:
            return ""
    except Exception:
        return ""
    by = {}
    for p, c in zip(st["parts"], counts):
        by[p["name"]] = by.get(p["name"], 0) + c
    rest = max(total - sum(by.values()), 0)
    bits = [f"{n} ~{round(q['grams'] * c / total)} g" for n, c in by.items()]
    if rest > 0.04 * total:
        bits.append(f"other/unchanged ~{round(q['grams'] * rest / total)} g")
    return ", ".join(bits)


def _store_cfg(acct):
    s = accounts.store_for(acct)
    return s, (s.cfg() if s else DEFAULT_CFG)


def create_quote(st, item, size, qty, due, changes, customer, acct):
    nocard = gr.update(value=None, visible=False)
    if not item:
        return (st, "Please pick the item.", *nav(3), None, None, "", "", "", nocard)
    store, cfg = _store_cfg(acct)
    names = [p["name"] for p in st["parts"]]
    ex = {"item_ids": [item], "requested_colors": names, "quantity": qty,
          "requested_size": (size or None) if _wearable(item) else None, "other_changes": changes or "", "missing_info": []}
    res = process(ex, customer or "", False, (due or "").strip() or None, cfg=cfg)
    q = res["quote"]
    if not q:
        return (st, "No quote: " + "; ".join(res["questions"]), *nav(3), None, None, "", "", "", nocard)
    # your agreed orders come first: ready date and warnings take the queue into account
    heads_up, queued = [], 0
    if store:
        ords = ordersmod.read_orders(store.orders_path)
        hrs = q["hours"] * q["qty"]
        queued = sum(1 for o in ords if ordersmod.is_committed(o))
        res["earliest"] = max(res["earliest"], ordersmod.queue_earliest(ords, hrs, cfg))
        try:
            wanted = datetime.date.fromisoformat((due or "").strip())
        except ValueError:
            wanted = None
        if wanted:
            res["due"] = wanted
            res["due_warning"] = (f"Customer wants it by {wanted}, but at your pace the earliest is {res['earliest']}"
                                  + (" (counting your agreed orders)." if queued else ".")) if wanted < res["earliest"] else None
            heads_up = ordersmod.workload_warnings(ords, hrs, wanted, cfg)
        else:
            res["due"] = res["earliest"]
    preview = st["current"] if st["parts"] else None
    res["has_preview"] = bool(preview)
    res["yarn_text"] = yarn_split(st, q) if preview else ""
    st.update(res=res, customer=customer or "", logged=False)
    md = (f"<div class='price-card'><div class='price-item'>{q['item']}</div><div class='price-big'>Rs {q['total_price']:,}</div></div>\n\n"
          f"- Grams {q['grams']} | Hours {q['hours']} | Cost Rs {q['cost']:,} | Markup {q['markup']}\n"
          f"- Earliest ready date: {res['earliest']:%d %b %Y}" + (f" (counts your {queued} agreed order{'s' if queued != 1 else ''})" if queued else "") + "\n")
    if res.get("yarn_text"): md += f"- Yarn by colour (estimate from picture area): {res['yarn_text']}\n"
    if res["due_warning"]: md += f"- **{res['due_warning']}**\n"
    for m in heads_up: md += f"- ⚠️ **{m}**\n"
    after = gr.update(value=preview, visible=bool(preview))
    return (st, "", *nav(4), st["base"] or st["photo"], after, md, make_reply(res, customer or ""), "", nocard)


def log_it(st, acct):
    if not st.get("res"): return st, "Nothing to log yet."
    store = accounts.store_for(acct)
    if store is None: return st, "🔒 Sign in (Account tab) to save orders. Your quote and card still work without it."
    if st["logged"]: return st, "Already logged."
    try:
        log_result(st["res"], st["customer"], path=store.orders_path, cfg=store.cfg())
    except RuntimeError as e:
        return st, str(e)
    st["logged"] = True
    return st, "Logged to orders.xlsx." if store.user is None else "Saved to your orders."


def card_it(st, acct):
    """Makes the shareable picture: before/after, price and ready date."""
    res = st.get("res")
    if not res or not res.get("quote"): return gr.update(), "Make the quote first."
    store = accounts.store_for(acct)
    shop = accounts.DEFAULT_SHOP                                   # brand name and logo are fixed
    logo = os.path.join(HERE, "assets", "sloth_logo.png")
    q = res["quote"]
    line1 = " · ".join(([f"For {_age_text(q['size_to'])}"] if q["size_to"] is not None else []) + ([f"{q['qty']} x Rs {q['unit_price']:,}"] if q["qty"] > 1 else []))
    cols = colour_text(res).strip()[3:] if colour_text(res) else ""
    details = [d for d in (line1, ("In " + cols) if cols else "") if d]
    ready = res["earliest"] if res.get("due_warning") else res["due"]
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"card_{uuid.uuid4().hex[:12]}.png")
    try:
        quotecard.make_card(out, shop, logo, st["base"] or st["photo"], st["current"] if st["parts"] else None, q["item"],
                            f"Rs {q['total_price']:,}", details, f"Ready by about {ready:%d %b %Y}", st.get("customer", ""))
    except Exception as e:
        return gr.update(), f"Could not make the card: {e}"
    return gr.update(value=out, visible=True), "Quote card ready. Use the download icon on the picture to save it."


# ---------- orders tab ----------
ORDER_COLS = ["#", "Date", "Customer", "Item", "Qty", "Colours", "Price (Rs)", "Status", "Due", "Heads-up"]


def orders_view(acct, search="", status="All"):
    """table rows, summary, status picker, workload note"""
    store = accounts.store_for(acct)
    if store is None:
        return [], "🔒 **Sign in to see and manage your orders** (Account tab).", gr.update(choices=[], value=None), ""
    cfg = store.cfg()
    ords = ordersmod.read_orders(store.orders_path)
    today = datetime.date.today()
    over = {o["row"] for o in ordersmod.overdue(ords, today)}
    risk = {o["row"] for o in ordersmod.late_orders(ords, cfg, today)}
    counts, booked = ordersmod.summary(ords, today)
    summ = " · ".join(f"{k} **{v}**" for k, v in counts.items()) + f"\n\nBooked this month (agreed + done): **Rs {booked:,.0f}**"
    s = (search or "").strip().lower()
    shown = [o for o in ords if (status in (None, "", "All") or o["status"] == status)
             and (not s or s in f"{o.get('Customer','')} {o.get('Item','')} {o.get('Colors requested','')}".lower())]
    rows = []
    for o in shown:
        flag = "⚠ overdue" if o["row"] in over else ("⚠ at risk" if o["row"] in risk else "")
        rows.append([o["row"], str(o.get("Date") or ""), o.get("Customer") or "", o.get("Item") or "", o.get("Qty") or "",
                     o.get("Colors requested") or "", o.get("Price (Rs)") or "", o["status"], str(o["due"] or ""), flag])
    pick = gr.update(choices=[(f"#{o['row']} · {o.get('Customer') or 'No name'} · {o.get('Item')} · {o['status']}", o["row"]) for o in shown], value=None)
    h, hpd = ordersmod.committed_hours(ords), float(cfg["schedule"]["hours_per_day"]) or 1.0
    work = ""
    if h > 0:
        work = (f"**Your queue:** {sum(1 for o in ords if ordersmod.is_committed(o))} agreed order(s), about {h:g} hours "
                f"({math.ceil(h / hpd)} days of crocheting at {hpd:g} h a day). Booked until about "
                f"**{ordersmod.queue_earliest(ords, 0, cfg):%d %b}**.")
        late = [o for o in ords if o["row"] in risk or o["row"] in over]
        for o in late:
            work += f"\n\n⚠️ **{o.get('Customer') or 'An order'}'s {o.get('Item')}** (due {o['due']:%d %b}) " + \
                    ("is overdue." if o["row"] in over else "will not be ready by then at your daily hours.")
    return rows, summ, pick, work


def orders_set_status(acct, row, new_status, search, status):
    store = accounts.store_for(acct)
    if store is None: return (*orders_view(acct, search, status), "Sign in first.")
    if not row: return (*orders_view(acct, search, status), "Pick an order first.")
    err = ordersmod.update_status(store.orders_path, row, new_status)
    return (*orders_view(acct, search, status), err or f"Order #{row} is now **{new_status}**.")


# ---------- price settings tab ----------
def settings_load(acct):
    store = accounts.store_for(acct)
    if store is None:
        return [gr.update()] * len(accounts.SETTING_FIELDS) + ["🔒 **Sign in to set your own prices** (Account tab). Guests see the shop's default prices in quotes."]
    return [*accounts.get_settings(store), ""]


def settings_save(acct, *vals):
    store = accounts.store_for(acct)
    if store is None: return "Sign in first."
    return accounts.save_settings(store, vals)


def settings_reset(acct):
    store = accounts.store_for(acct)
    if store is None: return [gr.update()] * len(accounts.SETTING_FIELDS) + ["Sign in first."]
    accounts.reset_settings(store)
    return [*accounts.get_settings(store), "Prices reset to the shop defaults."]


# ---------- account tab ----------
def do_sign_in(acct, username, password):
    u, msg = accounts.sign_in(username, password)
    return ({"user": u} if u else acct), msg, ""


def do_sign_up(acct, username, password):
    u, msg = accounts.sign_up(username, password)
    return ({"user": u} if u else acct), msg, ""


def do_sign_out(acct):
    return {"user": None}, "Signed out.", ""


def new_order():
    return (new_state(), *nav(0), None, "", "")


# ---------- layout ----------
import base64, itertools
_uid = itertools.count(1)
CREAM = "#fffbf7"


def _ball(cx, cy, r, fill, n=8, rot=-28, line=CREAM):
    u = next(_uid)
    ls = []
    for i in range(n):
        o = (i - (n - 1) / 2) * r * 0.27
        ls.append(f"<path d='M{-r*1.2:.1f},{o:.1f} Q0,{o + r*0.55:.1f} {r*1.2:.1f},{o:.1f}'/>")
    cross = "".join(f"<path d='M{-r*1.2:.1f},{(i-2)*r*.42:.1f} Q0,{(i-2)*r*.42 - r*.4:.1f} {r*1.2:.1f},{(i-2)*r*.42:.1f}'/>" for i in range(5))
    return (f"<g transform='translate({cx},{cy})'><clipPath id='c{u}'><circle r='{r}'/></clipPath><circle r='{r}' fill='{fill}'/>"
            f"<g clip-path='url(#c{u})' fill='none' stroke='{line}' stroke-width='{max(r*.045, 2):.1f}' stroke-linecap='round'>"
            f"<g transform='rotate({rot})'>{''.join(ls)}</g><g transform='rotate({rot+80})' opacity='.28'>{cross}</g></g></g>")


def _skein(cx, cy, rx, ry, rot, fill, line=CREAM):
    u = next(_uid)
    ls = "".join(f"<path d='M{-rx*1.3:.1f},{y+12} Q0,{y-14} {rx*1.3:.1f},{y+12}'/>" for y in range(int(-ry), int(ry), 15))
    return (f"<g transform='translate({cx},{cy}) rotate({rot})'><clipPath id='s{u}'><rect x='{-rx}' y='{-ry}' width='{rx*2}' height='{ry*2}' rx='{rx}'/></clipPath>"
            f"<rect x='{-rx}' y='{-ry}' width='{rx*2}' height='{ry*2}' rx='{rx}' fill='{fill}'/>"
            f"<g clip-path='url(#s{u})' fill='none' stroke='{line}' stroke-width='3.2' stroke-linecap='round'>{ls}"
            f"<path d='M0,{-ry} L0,{-ry+22} M0,{ry} L0,{ry-22}' stroke-width='6'/></g></g>")


def _button(cx, cy, r, fill, line=CREAM):
    d = r * 0.3
    dots = "".join(f"<circle cx='{x*d:.1f}' cy='{y*d:.1f}' r='{r*.1:.1f}' fill='{line}'/>" for x, y in ((-1, -1), (1, -1), (-1, 1), (1, 1)))
    return (f"<g transform='translate({cx},{cy})'><circle r='{r}' fill='{fill}'/><circle r='{r*.72:.1f}' fill='none' stroke='{line}' stroke-width='{max(r*.08, 1.4):.1f}'/>{dots}</g>")


def _scissors(cx, cy, rot, fill):
    return (f"<g transform='translate({cx},{cy}) rotate({rot})' fill='none' stroke='{fill}' stroke-linecap='round'>"
            f"<ellipse cx='-24' cy='-38' rx='17' ry='27' stroke-width='9' transform='rotate(18 -24 -38)'/>"
            f"<ellipse cx='24' cy='-38' rx='17' ry='27' stroke-width='9' transform='rotate(-18 24 -38)'/>"
            f"<path d='M-14,-8 L16,98 M14,-8 L-16,98' stroke-width='9'/></g>")


def _curl(x, y, s, col, w=7):
    return (f"<path transform='translate({x},{y}) scale({s})' d='M0,0 C40,-30 70,10 40,28 C14,42 -8,14 20,2 C46,-10 66,24 44,48' "
            f"fill='none' stroke='{col}' stroke-width='{w}' stroke-linecap='round'/>")


def _uri(w, h, body):
    svg = f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}'>{body}</svg>"
    return "url(\"data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode() + "\")"


def _cluster(flip=False):
    D, M, L, P = "#c4999a", "#b88a88", "#a77571", "#e3c9c8"
    body = (_scissors(268, 34, -20, M)
            + _ball(120, 98, 92, D) + _ball(318, 118, 58, P, rot=-10) + _ball(372, 84, 50, "#d9b9b8", rot=-40)
            + _skein(118, 318, 40, 108, 38, L)
            + _button(238, 150, 14, "#c4999a") + _button(62, 232, 13, "#cfa9a8") + _button(196, 262, 11, "#d8b8b6") + _button(452, 40, 11, "#d8b8b6")
            + _button(30, 332, 10, "#e0c4c2") + _button(172, 186, 8, "#b88a88"))
    if flip:
        body = f"<g transform='translate(520,0) scale(-1,1)'>{body}</g>"
    return _uri(520, 440, body)


def deco_css():
    left, right = _cluster(), _cluster(flip=True)
    curl_bl = _uri(160, 220, _curl(20, 40, 1.5, "#c4999a", 6) + _curl(8, 128, 1.1, "#d3b0ae", 6))
    curl_r = _uri(150, 170, _button(96, 120, 22, "#c4999a") + _curl(20, 30, 1.0, "#b88a88", 5))
    return f"""
html {{background:#fffbf7 !important;}}
body {{background-color:#fffbf7 !important; background-repeat:no-repeat !important; background-attachment:fixed !important;
  background-image:{left},{right},{curl_bl},{curl_r} !important;
  background-position:left top,right top,left bottom,right 58% !important;
  background-size:clamp(250px,34vw,520px) auto,clamp(250px,34vw,520px) auto,clamp(70px,9vw,140px) auto,clamp(60px,8vw,120px) auto !important;}}
@media (max-width: 800px) {{
  body {{background-size:46vw auto,46vw auto,0 0,0 0 !important;}}
}}
"""


def _text_color(h):
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return "#2b1d14" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#fffaf1"


STYLE = """
:root {color-scheme: light; --ink:#4a0a0a; --terra:#7a1f1f; --terra-d:#5c0505; --line:#ecd9d4; --paper:#fffdfb; --rose:#c4999a;}
body, gradio-app, .app, .gradio-container, .gradio-container > .main {background:transparent !important; background-image:none !important; color:var(--ink);}
.gradio-container {position:relative; z-index:2; width:100% !important; max-width:1120px !important; margin:0 auto !important;}
.tiny {height:0 !important; min-height:0 !important; padding:0 !important; margin:0 !important; border:0 !important; overflow:hidden !important; gap:0 !important;}
.tight, .tight > div, .tight .prose, .tight .html-container {padding:0 !important; margin:0 !important; min-height:0 !important;}
.gradio-container h1, .gradio-container h2, .gradio-container h3, .serif, .sec-title {font-family:'Young Serif','Georgia',serif !important; color:var(--ink); font-weight:400;}

/* header: centred logo, wordmark and tagline between two rules (yarn decoration lives on the page background) */
#hero {position:relative; display:flex; flex-direction:column; align-items:center; text-align:center; margin:26px 8px 26px 8px; color:var(--ink);}
#hero .hero-text {display:flex; flex-direction:column; align-items:center;}
#hero h1 {margin:6px 0 8px 0; font-size:2.3rem; line-height:1.1; color:var(--ink); letter-spacing:.03em;}
#hero h1 span {display:inline-block; cursor:default;}
#hero h1 span:hover {animation: hop .5s ease;}
#hero p {margin:0; display:flex; align-items:center; gap:14px; font-family:'Nunito',sans-serif; font-weight:700; font-size:1.05rem; color:var(--ink);}
#hero p::before, #hero p::after {content:""; width:clamp(26px,7vw,96px); height:1.5px; background:var(--ink);}
.logo-wrap {position:relative; width:150px; height:150px; cursor:pointer; filter:drop-shadow(0 6px 8px rgba(92,5,5,.22));}
.logo-wrap img {width:100%; height:100%; object-fit:contain; transform-origin:50% 55%; animation: sway 4s ease-in-out infinite;}
.logo-wrap:hover img {animation: spin 1.3s linear infinite;}
.logo-wrap:active {transform:scale(.92);}
@keyframes hop {0%{transform:translateY(0);} 40%{transform:translateY(-12px) rotate(-6deg);} 100%{transform:translateY(0);}}
@keyframes sway {0%,100%{transform:rotate(-6deg);} 50%{transform:rotate(6deg);}}
@keyframes spin {to{transform:rotate(360deg);}}
@keyframes wiggle {0%,100%{transform:rotate(0);} 25%{transform:rotate(-1.4deg);} 75%{transform:rotate(1.4deg);}}
@keyframes popin {from{opacity:0; transform:translateY(14px) scale(.98);} to{opacity:1; transform:none;}}

/* cards: soft white panels that sit in the middle of the page */
.card {background:#fffdfb !important; animation: popin .45s ease both; border:1px solid #ecd9d4 !important; border-radius:30px !important; padding:26px !important;
  margin:6px 10px 24px 10px !important; box-shadow: 0 14px 34px rgba(92,5,5,.10) !important;}
.narrow {width:100% !important; max-width: 700px; margin-left:auto !important; margin-right:auto !important;}

/* progress: five cross-stitches */
.stitches {display:flex; align-items:center; gap:7px; margin-bottom:10px;}
.stitches .x {width:26px; height:26px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  font-size:.8rem; font-weight:800; border:2px dashed #dcb9b6; color:#cfaeab;}
.stitches .x.done {border-style:solid; border-color:var(--terra); color:var(--terra);}
.stitches .x.now {background:var(--terra-d); border:2px solid var(--terra-d); color:#fffbf7; box-shadow:0 0 0 4px rgba(92,5,5,.15);}
.stitches .lbl {margin-left:8px; font-weight:800; font-size:.8rem; letter-spacing:.09em; text-transform:uppercase; color:var(--terra-d);}

.sec-title {font-size:1.2rem; margin:2px 0 2px 0;}
.hint {color:#8a5a58; font-size:.92rem; margin:0 0 6px 0;}

/* pill buttons with a soft shadow, maroon for the main action */
.gradio-container button {border-radius:999px !important; font-weight:700 !important; transition: transform .08s ease, box-shadow .15s ease;}
.gradio-container button.primary {background:var(--terra-d) !important; border-color:var(--terra-d) !important; color:#fffbf7 !important; box-shadow:0 6px 14px rgba(92,5,5,.28);}
.gradio-container button:active {transform:scale(.95);}
.gradio-container button:hover {transform: translateY(-2px); box-shadow: 0 6px 16px rgba(92,5,5,.22);}

/* palette cards */
.pal-grid {padding:0 2px; display:grid !important; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px !important; margin:6px 0 12px 0;}
.pal-card {display:block !important; width:100% !important; min-width:0 !important; height:auto !important; text-align:left !important;
  white-space:pre-line !important; line-height:1.35 !important; font-weight:500 !important; font-size:.84rem !important; border-radius:22px !important;
  background:#fffdfb !important; color:#7a5352 !important; border:2px solid #efdcd8 !important; padding:14px 14px 12px 14px !important;}
.pal-card::first-line {font-family:'Young Serif','Georgia',serif; font-weight:400; font-size:1.02rem; color:var(--ink);}
.pal-card:hover {border-color:var(--terra) !important; animation: wiggle .45s ease;}
.pal-card::after {content:""; display:block; margin-top:11px; width:124px; height:34px;
  background:
    radial-gradient(circle at 17px 17px,  var(--c1,transparent) 0 14px, var(--g1,transparent) 14.5px 16px, transparent 16.5px),
    radial-gradient(circle at 62px 17px,  var(--c2,transparent) 0 14px, var(--g2,transparent) 14.5px 16px, transparent 16.5px),
    radial-gradient(circle at 107px 17px, var(--c3,transparent) 0 14px, var(--g3,transparent) 14.5px 16px, transparent 16.5px);}

/* colour chips of the active palette and the 30 swatches */
.chip-row {flex-wrap:wrap !important; gap:10px !important;}
.pal-chip {flex:0 0 auto !important; min-width:0 !important; width:auto !important; padding:10px 22px !important; font-size:1rem !important;
  border-radius:999px !important; border:2px solid rgba(60,20,20,.3) !important;}
.swatch-grid {flex-wrap:wrap !important; gap:8px !important;}
.swatch-grid button {flex:0 0 auto !important; min-width:0 !important; width:auto !important; border-radius:999px !important;
  padding:6px 14px !important; font-size:.85rem !important;}
.picker-box {background:#fdf3f0 !important; border:2px dashed #ecd0cb !important; border-radius:22px !important; padding:14px !important;}

/* the photo: pinned on with washi tape */
.work-row {align-items:flex-start !important; gap:26px !important;}
.right-col {position:sticky; top:14px; align-self:flex-start;}
.photo-frame {position:relative; background:#fffdfb !important; padding:14px !important; border:none !important; border-radius:8px !important;
  box-shadow:0 10px 26px rgba(92,5,5,.20) !important; transform:rotate(.6deg);}
.photo-frame::before {content:""; position:absolute; top:-13px; left:50%; width:100px; height:28px; transform:translateX(-50%) rotate(-2deg); z-index:5;
  background:rgba(196,153,154,.65); border-left:2px dashed rgba(255,255,255,.7); border-right:2px dashed rgba(255,255,255,.7);}
.photo-frame img {border-radius:4px;}

.price-card {background:linear-gradient(135deg,#fdf1ee,#fffaf8); border:2px dashed #ecd0cb; border-radius:22px; padding:16px 22px; margin-bottom:8px;}
.price-item {color:#7a5352; font-weight:700;}
.price-big {font-family:'Young Serif','Georgia',serif; font-size:2.3rem; font-weight:400; color:var(--terra-d); line-height:1.15;}

@media (max-width: 800px) {
  .work-row {flex-direction:column-reverse !important;}
  .work-row > * {width:100% !important; min-width:0 !important; flex:none !important;}
  .right-col {position:static;}
  #hero {margin-top:18px;}
  #hero h1 {font-size:1.7rem !important;}
  #hero p {font-size:.92rem !important;}
  .logo-wrap {width:112px; height:112px;}
  .pal-card {padding:12px 10px !important;}
  .pal-card::after {transform:scale(.8); transform-origin:left center; margin-top:8px;}
  .pal-grid {grid-template-columns: 1fr 1fr;}
}
/* top tabs as pills */
.gradio-container button[role="tab"] {border-radius:999px !important; font-weight:800 !important; padding:8px 20px !important; margin:0 4px 6px 0; border:2px solid #ecd0cb !important; background:#fdf3f0 !important; color:#5c0a0a !important;}
.gradio-container button[role="tab"][aria-selected="true"] {background:#5c0505 !important; color:#fffbf7 !important; border-color:#5c0505 !important; box-shadow:0 6px 14px rgba(92,5,5,.25);}
.gradio-container .tab-wrapper, .gradio-container .tab-nav, .gradio-container .tab-container, .gradio-container div[role="tablist"] {border-bottom:none !important;}
.gradio-container div[role="tablist"] {padding:12px 0 10px 0 !important; gap:6px;}
.gradio-container .tab-wrapper::after, .gradio-container .tab-container::after, .gradio-container .tab-wrapper::before {display:none !important; background:none !important; height:0 !important;}
.gradio-container .tab-container, .gradio-container div[role="tablist"] {justify-content:center !important; overflow:visible !important; flex-wrap:wrap !important;}
@media (max-width: 800px) {
  .gradio-container {padding-left:4px !important; padding-right:4px !important;}
  .gradio-container button[role="tab"] {padding:5px 7px !important; font-size:.7rem !important; margin:0 !important; border-width:1.5px !important;}
  .gradio-container div[role="tablist"] {gap:2px; padding:8px 0 6px 0 !important;}
}
.gradio-container .tabitem {border:none !important; background:transparent !important; padding:0 !important;}
.gradio-container .dataframe, .gradio-container table {border-radius:14px;}
""" + deco_css()
CSS = "<style>" + STYLE + "".join(
    f"#sw-{i}, #sw-{i} button {{background:{h} !important; color:{_text_color(h)} !important; border:1px solid rgba(0,0,0,.25) !important;}}"
    for i, (n, h) in enumerate(PALETTE)) + "</style>"
FONTS = "<link href='https://fonts.googleapis.com/css2?family=Young+Serif&family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700;800&display=swap' rel='stylesheet'>"

LABELS = ["Photo", "Colours", "Colours", "Details", "Result"]


def chip(n, label):
    xs = "".join(f"<span class='x {'done' if i < n else 'now' if i == n else ''}'>{'✕' if i < n else i}</span>" for i in range(1, 6))
    return gr.HTML(f"<div class='stitches'>{xs}<span class='lbl'>{label}</span></div>")


import base64
TITLE = "KARSAZ Stitch Desk"   # <- change the heading text here
WAVE_TITLE = "".join("<span>&nbsp;</span>" if c == " " else f"<span>{c}</span>" for c in TITLE)
try:
    with open(os.path.join(HERE, "assets", "sloth_logo.png"), "rb") as _f:
        LOGO_IMG = "<img id='logo-img' alt='Sleepy sloth logo' src='data:image/png;base64," + base64.b64encode(_f.read()).decode() + "'>"
except OSError:
    LOGO_IMG = "<span class='ball'>🧶</span>"   # logo file missing: fall back to an emoji


import inspect
_TERRA = gr.themes.Color(c50="#fdf6f4", c100="#f6e6e2", c200="#ecd0ca", c300="#d9aaa3", c400="#b9756f", c500="#7a1f1f",
                         c600="#5c0a0a", c700="#4d0808", c800="#3d0606", c900="#2e0505", c950="#1f0303")
THEME = gr.themes.Soft(primary_hue=_TERRA, secondary_hue="amber", neutral_hue="stone",
                       font=[gr.themes.GoogleFont("Nunito"), "ui-sans-serif", "system-ui", "sans-serif"])
try:  # keep the cosy cream look in dark mode too
    THEME = THEME.set(
        body_background_fill="#fffbf7", body_background_fill_dark="#fffbf7",
        body_text_color="#4a0a0a", body_text_color_dark="#4a0a0a",
        block_background_fill="#fffdfb", block_background_fill_dark="#fffdfb",
        block_border_color="#ecd9d4", block_border_color_dark="#ecd9d4",
        input_background_fill="#fffdfb", input_background_fill_dark="#fffdfb",
        button_secondary_background_fill="#fdf3f0", button_secondary_background_fill_dark="#fdf3f0",
        button_secondary_background_fill_hover="#f9e4df", button_secondary_background_fill_hover_dark="#f9e4df",
        button_secondary_text_color="#5c0a0a", button_secondary_text_color_dark="#5c0a0a",
        button_secondary_border_color="#ecd0cb", button_secondary_border_color_dark="#ecd0cb")
except Exception:
    pass
_new_gradio = "theme" in inspect.signature(gr.Blocks.launch).parameters  # newer Gradio moved theme/css to launch()
_blocks_kw = {} if _new_gradio else {"theme": THEME, "css": STYLE}
_tabs_kw = {"overflow_behavior": "wrap"} if "overflow_behavior" in inspect.signature(gr.Tabs.__init__).parameters else {}   # phones: wrap the tabs instead of hiding them in a menu

with gr.Blocks(title="Crochet order assistant", **_blocks_kw) as demo:
    gr.HTML(FONTS + CSS)
    gr.HTML("<div id='hero'><div class='logo-wrap' id='logo'>" + LOGO_IMG + "</div><div class='hero-text'>"
            "<h1 class='wave'>" + WAVE_TITLE + "</h1><p>Photo in, colours chosen, price and reply out.</p></div></div>")
    st = gr.State(new_state())
    acct = gr.State({"user": None})

    with gr.Tabs(**_tabs_kw):
      with gr.Tab("🧶 New order"):
            # ---- 1. photo ----
            with gr.Column(visible=True, elem_classes="card narrow") as s1:
                chip(1, "Photo")
                photo_in = gr.Image(type="filepath", label="Upload the reference photo", height=360)
                msg_in = gr.Textbox(label="Customer's message (optional)", lines=2,
                                    placeholder="e.g. Want this for a 1 year old in mint green")
                s1_msg = gr.Markdown()
                btn_start = gr.Button("Continue", variant="primary", size="lg")

            # ---- 2. colour change? ----
            with gr.Column(visible=False, elem_classes="card narrow") as s2:
                chip(2, "Colours")
                gr.Markdown("### Does the customer want a colour change?")
                s2_info = gr.Markdown()
                with gr.Row():
                    btn_yes = gr.Button("🎨 Yes, change the colour", variant="primary", size="lg")
                    btn_no = gr.Button("No, keep the original", size="lg")

            # ---- 3. colours: choices on the left, photo on the right ----
            with gr.Column(visible=False, elem_classes="card") as s3:
                chip(3, "Choose colours")
                with gr.Row(elem_classes="work-row"):
                    with gr.Column(scale=5, min_width=330):
                        s3_status = gr.Markdown()
                        with gr.Column(visible=False, elem_classes="picker-box") as palette_box:
                            gr.HTML("<div class='sec-title'>Colour this part</div>")
                            with gr.Row(elem_classes="chip-row"):
                                chips = [gr.Button("", visible=False, elem_id=f"pc-{k}", elem_classes="pal-chip") for k in range(3)]
                            gr.Markdown("**More colours**")
                            swatches = []
                            with gr.Row(elem_classes="swatch-grid"):
                                for i in range(len(PALETTE)):
                                    swatches.append((i, gr.Button(PALETTE[i][0], elem_id=f"sw-{i}", size="sm")))
                            with gr.Accordion("Any other colour", open=False):
                                with gr.Row():
                                    picker = gr.ColorPicker(label="Pick a colour", value="#9ED9B8")
                                    btn_picker = gr.Button("Use this colour")
                            btn_clear = gr.Button("Cancel selection")
                        pal_note = gr.HTML(elem_classes="tight")
                        pal_css = gr.HTML(elem_classes="tiny")
                        with gr.Row(elem_classes="pal-grid"):
                            pal_btns = [gr.Button("", visible=False, elem_id=f"pal-{i}", elem_classes="pal-card") for i in range(6)]
                        with gr.Accordion("Fine-tune the selection", open=False):
                            mode = gr.Radio(["Add area", "Remove area", "Draw shape"], value="Add area", label="Each click will")
                            with gr.Row(visible=False) as shape_row:
                                btn_close = gr.Button("✔ Close shape", variant="primary")
                                btn_corner = gr.Button("↩ Undo corner")
                            natural = gr.Checkbox(value=True, label="Natural lighting finish (keeps the photo's light and shadow on the new colour)")
                            smart = gr.Checkbox(value=True, label="Smart selection (untick if the page freezes)")
                            gr.Markdown("Not clean enough? These adjust the last part:")
                            with gr.Row():
                                btn_more = gr.Button("Old colour left at the edge")
                                btn_less = gr.Button("Colour spilling outside")
                            with gr.Row():
                                btn_loose = gr.Button("Colour missing in places")
                                btn_strict = gr.Button("Spilling onto other parts")
                            btn_adjreset = gr.Button("Reset adjustments")
                        with gr.Row():
                            btn_undo = gr.Button("↩ Undo")
                            btn_reset = gr.Button("Start over")
                        btn_next = gr.Button("Next →", variant="primary", size="lg")
                    with gr.Column(scale=6, min_width=340, elem_classes="right-col"):
                        canvas = gr.Image(show_label=False, interactive=False, type="filepath", elem_classes="photo-frame")

            # ---- 4. details ----
            with gr.Column(visible=False, elem_classes="card narrow") as s4:
                chip(4, "Details")
                gr.Markdown("### Size, deadline and details")
                dd_item = gr.Dropdown(CHOICES, value="", label="Item")
                with gr.Row():
                    tb_size = gr.Textbox(label="Size or age", placeholder="1 year old, 18 months")
                    num_qty = gr.Number(value=1, precision=0, label="Quantity")
                with gr.Row():
                    tb_due = gr.Textbox(label="Customer's deadline (optional)", placeholder="YYYY-MM-DD")
                    tb_name = gr.Textbox(label="Customer name (optional)")
                tb_changes = gr.Textbox(label="Other changes requested (separate with commas)")
                s4_msg = gr.Markdown()
                btn_quote = gr.Button("Create quote and reply", variant="primary", size="lg")

            # ---- 5. result ----
            with gr.Column(visible=False, elem_classes="card") as s5:
                chip(5, "Result")
                with gr.Row():
                    before5 = gr.Image(label="Before", interactive=False, elem_classes="photo-frame")
                    after5 = gr.Image(label="After", interactive=False, elem_classes="photo-frame")
                quote_md = gr.Markdown()
                reply_box = gr.Textbox(label="Reply draft (select, copy and edit)", lines=10)
                with gr.Row():
                    btn_log = gr.Button("📒 Log this order", variant="primary")
                    btn_card = gr.Button("🖼 Make quote card")
                    btn_new = gr.Button("New order")
                card_img = gr.Image(label="Quote card (use the download icon to save it)", interactive=False, visible=False)
                log_msg = gr.Markdown()


      # ---- orders ----
      with gr.Tab("📒 Orders") as tab_orders:
        with gr.Column(elem_classes="card"):
            gr.Markdown("### Your orders")
            gr.Markdown("**Quoted** = price sent · **Pending** = customer said yes · **In progress** · **Done** · **Cancelled**. "
                        "Only Pending and In progress count as your workload.")
            ord_sum = gr.Markdown()
            ord_work = gr.Markdown()
            with gr.Row():
                ord_search = gr.Textbox(label="Search customer, item or colour", scale=3)
                ord_filter = gr.Dropdown(["All"] + ordersmod.STATUSES, value="All", label="Show", scale=1)
                btn_ord_refresh = gr.Button("↻ Refresh", scale=1)
            ord_table = gr.Dataframe(headers=ORDER_COLS, value=[], interactive=False, wrap=True)
            with gr.Row():
                ord_pick = gr.Dropdown([], label="Pick an order", scale=3)
                ord_status = gr.Dropdown(ordersmod.STATUSES, value="In progress", label="Change status to", scale=2)
                btn_ord_set = gr.Button("Update status", variant="primary", scale=1)
            ord_msg = gr.Markdown()
      # ---- prices ----
      with gr.Tab("💰 Prices") as tab_prices:
        with gr.Column(elem_classes="card narrow"):
            gr.Markdown("### Your prices")
            gr.Markdown("New quotes use these numbers. Past orders keep the price they were logged with.")
            set_fields = [gr.Number(label=lbl) for _, lbl, _, _ in accounts.SETTING_FIELDS]
            with gr.Row():
                btn_set_save = gr.Button("Save prices", variant="primary")
                btn_set_reset = gr.Button("Reset to shop defaults")
            set_msg = gr.Markdown()
      # ---- account ----
      if accounts.ACCOUNTS_ON:
       with gr.Tab("👤 Account") as tab_acct:
        with gr.Column(elem_classes="card narrow"):
            gr.Markdown("### Sign in to save your orders and prices\n"
                        "You can make quotes, colour previews and quote cards without an account. "
                        "An account keeps your orders, prices and logo in your own private space.")
            acct_user = gr.Textbox(label="Username")
            acct_pw = gr.Textbox(label="Password", type="password")
            with gr.Row():
                btn_in = gr.Button("Sign in", variant="primary")
                btn_up = gr.Button("Create account")
                btn_out = gr.Button("Sign out")
            acct_msg = gr.Markdown("Not signed in (guest).")

    screens = [s1, s2, s3, s4, s5]
    view_out = [st, canvas, s3_status, palette_box]
    details_out = [st, *screens, dd_item, tb_size, num_qty, tb_changes, s4_msg]
    pal_out = [pal_css, *pal_btns, pal_note, *chips]

    btn_start.click(start, [photo_in, msg_in, st], [st, s1_msg, *screens, s2_info])
    btn_yes.click(yes, st, [st, *screens, canvas, s3_status, palette_box, *pal_out])
    btn_no.click(skip_colour, st, details_out)
    canvas.select(on_click, [st, mode, smart], view_out)
    for i, b in enumerate(pal_btns):
        b.click((lambda n: (lambda s: pick_palette(s, n)))(i), st, [st, canvas, s3_status, pal_css, *chips, palette_box])
    for k, b in enumerate(chips):
        b.click((lambda n: (lambda s: apply_chip(s, n)))(k), st, view_out)
    for i, b in swatches:
        name, hexv = PALETTE[i]
        b.click((lambda n, h: (lambda s: apply_colour(s, n, h)))(name, hexv), st, view_out)
    btn_picker.click(apply_picker, [st, picker], view_out)
    btn_clear.click(clear_selection, st, view_out)
    btn_undo.click(undo, st, view_out)
    btn_reset.click(start_over, st, view_out)
    for b, key in [(btn_more, "more"), (btn_less, "less"), (btn_loose, "loose"), (btn_strict, "strict"), (btn_adjreset, "reset")]:
        b.click((lambda k: (lambda s: adjust(s, k)))(key), st, view_out)
    btn_next.click(next_from_parts, st, details_out)
    mode.change(mode_changed, mode, shape_row)
    btn_close.click(close_shape, st, view_out)
    btn_corner.click(undo_corner, st, view_out)
    natural.change(natural_toggle, [st, natural], view_out)
    dd_item.change(size_visibility, dd_item, tb_size)      # picking another item shows or hides the size field
    btn_quote.click(create_quote, [st, dd_item, tb_size, num_qty, tb_due, tb_changes, tb_name, acct],
                    [st, s4_msg, *screens, before5, after5, quote_md, reply_box, log_msg, card_img])
    btn_log.click(log_it, [st, acct], [st, log_msg])
    btn_card.click(card_it, [st, acct], [card_img, log_msg])

    ord_out = [ord_table, ord_sum, ord_pick, ord_work]
    ord_in = [acct, ord_search, ord_filter]
    tab_orders.select(orders_view, ord_in, ord_out)
    btn_ord_refresh.click(orders_view, ord_in, ord_out)
    ord_search.submit(orders_view, ord_in, ord_out)
    ord_filter.change(orders_view, ord_in, ord_out)
    btn_ord_set.click(orders_set_status, [acct, ord_pick, ord_status, ord_search, ord_filter], [*ord_out, ord_msg])

    set_out = [*set_fields, set_msg]
    tab_prices.select(settings_load, acct, set_out)
    btn_set_save.click(settings_save, [acct, *set_fields], set_msg)
    btn_set_reset.click(settings_reset, acct, set_out)

    if accounts.ACCOUNTS_ON:
        btn_in.click(do_sign_in, [acct, acct_user, acct_pw], [acct, acct_msg, acct_pw])
        btn_up.click(do_sign_up, [acct, acct_user, acct_pw], [acct, acct_msg, acct_pw])
        btn_out.click(do_sign_out, acct, [acct, acct_msg, acct_pw])
    btn_new.click(new_order, None, [st, *screens, photo_in, msg_in, s1_msg])

if __name__ == "__main__":
    if _new_gradio:
        demo.launch(theme=THEME, css=STYLE)
    else:
        demo.launch()