"""Recolor v4: customer-ready colour swap.
  Recolorer(path)  -> finds the item once (the slow part, uses rembg)
  .render(hex, grow, tol, fill) -> recolors in about a second

How it works (saturated items such as red, blue, green):
  every pixel is treated as a MIX of "garment colour" and "whatever is behind it" (white pom-pom, cream cloth).
  The share of garment colour is estimated from colour saturation and hue. Only that share is recoloured,
  so edge pixels do not keep a red outline and white parts stay white.
White, grey or black items have no saturation to measure, so they use the older colour-distance method.

Knobs:  grow = how far beyond the cut-out edge to look for stray old-colour pixels (pixels)
        tol  = colour-match looseness (1.0 normal, higher recolors more shades)
        fill = size of small gaps/dots inside the item that get filled (pixels)
"""
import numpy as np
import cv2
from PIL import Image, ImageOps

_session = None


def _rembg_alpha(img):
    global _session
    from rembg import remove, new_session
    if _session is None:
        _session = new_session("isnet-general-use")
    return np.asarray(remove(img, session=_session))[..., 3].astype(np.float32) / 255.0


def _hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _ellipse(n):
    n = max(int(n), 1)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))


def _smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def _masked_blur(x, w, sigma):
    """Blur x using only pixels where w is large, so the paper around the part does not leak into it."""
    return cv2.GaussianBlur(x * w, (0, 0), sigma) / np.maximum(cv2.GaussianBlur(w, (0, 0), sigma), 1e-3)


def _shade(hex_color, lumG, core, weight=None, natural=False):
    """Stitch shading for the new colour (v5.1).
    1. STITCH DETAIL: soft or pale source yarn gives a weak brightness signal, and a dark target shrinks it
       further, so the stitches blur. The stitch-scale detail is measured and amplified.
    2. FORM: the broad light and shadow across the part (the roundness of the petal) is also lifted when it is
       weak, otherwise the part looks flat, like a sticker.
    3. PALE TARGETS: highlights roll off smoothly instead of clipping to one flat colour."""
    ref = np.percentile(lumG[core], 60) if core.any() else 0.5
    s = np.clip(lumG / max(ref, 1e-3), 0, 1.4) ** 1.3
    w = core.astype(np.float32) if weight is None else np.clip(weight, 0, 1).astype(np.float32)

    big = _masked_blur(s, w, FORM_SIGMA)                    # roundness / broad light
    low = _masked_blur(s, w, 3.0)
    form = big - float(big[core].mean()) if core.any() else big * 0
    sd_f = float(form[core].std()) if core.any() else 0.1
    g_f = float(np.clip(FORM_TARGET / max(sd_f, 1e-3), 1.0, 2.5 if natural else MAX_FORM_GAIN))
    mid = low - big                                         # in-between lighting (folds)
    detail = s - low                                        # stitch scale
    sd = float(detail[core].std()) if core.any() else 0.1
    gain = float(np.clip(DETAIL_TARGET / max(sd, 1e-3), 1.0, 3.0 if natural else MAX_GAIN))
    s = (1 + form * g_f) + mid * min(g_f, 2.0) + detail * gain

    peak = float(_hex_rgb(hex_color).max() / 255.0)
    head = max(1.0 / max(peak, 1e-3) - 1.0, 0.002)
    s = np.where(s > 1, 1 + head * np.tanh((s - 1) / head), s)
    return np.clip(s, 0, 1.6)[..., None]


def _paint(hex_color, s, natural=False):
    """Colour of the part in the new yarn. s = the shading from _shade (H x W x 1).
    Plain: new colour x shading (as before).
    Natural: the shading is applied to LIGHTNESS (perceptual), not to the RGB numbers. Shadows keep their colour and only
    get darker (like real dyed yarn); highlights wash out toward white (like real sheen) instead of staying a flat tint."""
    rgb = _hex_rgb(hex_color) / 255.0
    if not natural:
        return np.clip(rgb * s, 0, 1)
    Lt, at, bt = cv2.cvtColor(rgb.reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
    Lt = max(float(Lt), 4.0)
    ratio = np.clip(s[..., 0], 0, 1.6)
    Ln = np.clip(Lt * ratio, 0, 100)
    k = Ln / Lt
    cs = np.where(k < 1, k ** 0.45, np.maximum(k, 1e-3) ** -0.9)      # chroma: falls slowly in shadow, washes out in highlights
    lab = np.dstack([Ln, at * cs, bt * cs]).astype(np.float32)
    return np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1)


def _soften(f):
    """Gives the part an edge like real yarn instead of a sharp cut-out: only the edge band is feathered, and only
    inward, so no coloured halo spreads onto the background."""
    if EDGE_SOFT <= 0:
        return f
    soft = np.minimum(f, cv2.GaussianBlur(f, (0, 0), EDGE_SOFT))   # inward only: no colour halo on the background
    band = cv2.dilate(((f > 0.02) & (f < 0.98)).astype(np.uint8), _ellipse(5)).astype(np.float32)
    band = cv2.GaussianBlur(band, (0, 0), 1.0)
    return np.clip(f * (1 - band) + soft * band, 0, 1)


DETAIL_TARGET = 0.10   # wanted stitch detail (std of the shading). Raise for crisper stitches, lower for softer
MAX_GAIN = 4.0         # never amplify more than this (stops noise turning into grain)
FORM_SIGMA = 18        # size of the broad light/shadow (pixels). Larger = gentler roundness
FORM_TARGET = 0.12     # wanted broad light and shadow across the part. 0 turns the form lift off
MAX_FORM_GAIN = 3.5
EDGE_SOFT = 1.4        # edge feathering in pixels. 0 = sharp cut-out edge, 2.5 = very soft

class Recolorer:
    def __init__(self, path, max_side=2000, alpha_fn=None):
        img = Image.open(path)
        self.icc = img.info.get("icc_profile")
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side))  # keeps adjustments quick on big phone photos
        self.rgb = np.asarray(img).astype(np.float32) / 255.0
        a = (alpha_fn or _rembg_alpha)(img)
        self.hard = (a > 0.5).astype(np.uint8)
        self.interior = cv2.erode(self.hard, _ellipse(9)).astype(bool)
        if not self.interior.any():
            raise ValueError("Could not find the item in the photo. Try a photo with a clearer background.")
        r = self.rgb
        self.lum = 0.2126 * r[..., 0] + 0.7152 * r[..., 1] + 0.0722 * r[..., 2]

        hsv = cv2.cvtColor(r, cv2.COLOR_RGB2HSV)  # H 0-360, S and V 0-1
        self.H, self.S = hsv[..., 0], hsv[..., 1]
        ring = (cv2.dilate(self.hard, _ellipse(41)) - cv2.dilate(self.hard, _ellipse(15))).astype(bool)
        sel = self.interior & (self.S > 0.45)
        self.saturated = sel.sum() > 0.05 * self.interior.sum()
        if self.saturated:
            ang = np.deg2rad(self.H[sel])
            self.hue0 = float(np.rad2deg(np.arctan2(np.median(np.sin(ang)), np.median(np.cos(ang)))) % 360)
            self.sR = float(np.median(self.S[sel]))
            self.sO = float(np.median(self.S[ring])) if ring.any() else 0.08
            self.O = np.median(r[ring], axis=0) if ring.any() else np.array([0.9, 0.9, 0.9], np.float32)
        else:  # fallback data: distance from the item's main colour in LAB
            lab = cv2.cvtColor((r * 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
            d = lab - np.median(lab[self.interior], axis=0)
            self.dist = np.sqrt(d[..., 1] ** 2 + d[..., 2] ** 2 + (0.25 * d[..., 0]) ** 2)

    # ---- saturated items: mix model ----
    def _render_mix(self, hex_color, grow, tol, fill):
        D = max(self.sR - self.sO, 0.05)
        f = np.clip((self.S - self.sO) / (D / tol), 0, 1)
        dh = np.abs((self.H - self.hue0 + 180) % 360 - 180)
        f *= 1 - _smooth(dh, 25 * min(tol, 1.5), 55 * min(tol, 1.5))

        # keep only colour regions connected to the item (drops pink lines on a face, red specks elsewhere)
        n, lab = cv2.connectedComponents((f > 0.08).astype(np.uint8), connectivity=8)
        strong = np.bincount(lab[f > 0.9].ravel(), minlength=n)
        keep_ids = strong >= max(200, 0.0005 * f.size)
        keep_ids[0] = False
        keep = cv2.GaussianBlur(keep_ids[lab].astype(np.float32), (0, 0), 1.0)

        # fill small gaps and dots (bright stitches) but not big areas like pom-poms
        f = np.maximum(f, cv2.morphologyEx(f, cv2.MORPH_CLOSE, _ellipse(fill)) * self.interior)
        grown = cv2.GaussianBlur(cv2.dilate(self.hard, _ellipse(grow)).astype(np.float32), (0, 0), 1.2)
        f = np.clip(f * keep * grown, 0, 1)

        # colour of whatever is next to the item (white pom-pom, cream cloth), measured locally
        other = (f < 0.05).astype(np.float32)
        num = cv2.GaussianBlur(self.rgb * other[..., None], (0, 0), 8)
        den = cv2.GaussianBlur(other, (0, 0), 8)
        O = np.where((den > 0.05)[..., None], num / np.maximum(den, 1e-3)[..., None], self.O)

        # edge pixels count as mostly garment, so no old colour is left behind
        f = np.maximum(f, _smooth(f, 0.12, 0.5))

        # recolour only the garment share of each pixel
        f = _soften(f)
        f3 = f[..., None]
        G = np.clip((self.rgb - (1 - f3) * O) / np.maximum(f3, 0.1), 0, 1)  # garment colour in the mix
        lumG = G.max(axis=2)  # strongest channel: ignores pink/white colour spill that fools plain brightness
        core = self.interior & (f > 0.9)
        shading = _shade(hex_color, lumG, core, f)
        Gp = np.clip((_hex_rgb(hex_color) / 255.0) * shading, 0, 1)
        out = np.clip(self.rgb + f3 * (Gp - G), 0, 1)

        # faint pink left on white parts right next to the item: pull the red back to neutral
        near = cv2.GaussianBlur(cv2.dilate((f > 0.5).astype(np.uint8), _ellipse(9)).astype(np.float32), (0, 0), 1.5)
        pink = np.maximum(out[..., 0] - 0.5 * (out[..., 1] + out[..., 2]), 0)
        w = near * (1 - f) * (1 - _smooth(dh, 40, 80))
        out[..., 0] = np.clip(out[..., 0] - w * pink * 0.9, 0, 1)
        return out

    # ---- white / grey / black items: colour distance ----
    def _render_distance(self, hex_color, grow, tol, fill):
        full, zero = 18 * tol, 40 * tol
        grown = cv2.GaussianBlur(cv2.dilate(self.hard, _ellipse(grow)).astype(np.float32), (0, 0), 1.5)
        similar = np.clip(1 - (self.dist - full) / (zero - full), 0, 1)
        similar = cv2.morphologyEx(similar, cv2.MORPH_CLOSE, _ellipse(fill))
        weight = (grown * similar)[..., None]
        core = self.interior & (similar > 0.5)
        ref = np.percentile(self.lum[core], 60) if core.any() else 0.5
        shading = np.clip(self.lum / max(ref, 1e-3), 0, 1.3)[..., None]
        new = np.clip((_hex_rgb(hex_color) / 255.0) * shading, 0, 1)
        return self.rgb * (1 - weight) + new * weight

    def render(self, hex_color, grow=9, tol=1.0, fill=9):
        fn = self._render_mix if self.saturated else self._render_distance
        out = fn(hex_color, int(grow), float(tol), int(fill))
        return Image.fromarray((out * 255).astype(np.uint8))

    def save(self, img, path):
        if self.icc:
            img.save(path, icc_profile=self.icc)
        else:
            img.save(path)



class SamClicker:
    """Segment Anything (via rembg). Encodes the photo once, then answers each click in well under a second."""

    def __init__(self, img):
        import rembg.sessions.sam as S
        from rembg import new_session
        self.S = S
        self.sess = new_session("sam")  # one-time download of about 375 MB
        cv = np.array(img.convert("RGB"))
        self.original_size = cv.shape[:2]
        self.input_size = (684, 1024)
        sc = min(self.input_size[1] / cv.shape[1], self.input_size[0] / cv.shape[0])
        self.T = np.array([[sc, 0, 0], [0, sc, 0], [0, 0, 1]])
        cv = S.warp_affine(cv, self.T[:2], (self.input_size[0], self.input_size[1]))
        name = self.sess.encoder.get_inputs()[0].name
        self.emb = self.sess.encoder.run(None, {name: cv.astype(np.float32)})[0]

    def segment(self, points):
        """points = [(x, y, label)], label 1 = this is part of it, 0 = this is not. Returns a bool mask."""
        S = self.S
        pts = np.array([[p[0], p[1]] for p in points], dtype=np.float32)
        lab = np.array([p[2] for p in points], dtype=np.float32)
        coord = np.concatenate([pts, np.array([[0.0, 0.0]])], axis=0)[None]
        label = np.concatenate([lab, [-1]])[None].astype(np.float32)
        coord = S.apply_coords(coord, self.input_size, 1024).astype(np.float32)
        coord = np.concatenate([coord, np.ones((1, coord.shape[1], 1), np.float32)], axis=2)
        coord = np.matmul(coord, self.T.T)[:, :, :2].astype(np.float32)
        masks, _, _ = self.sess.decoder.run(None, {
            "image_embeddings": self.emb, "point_coords": coord, "point_labels": label,
            "mask_input": np.zeros((1, 1, 256, 256), np.float32), "has_mask_input": np.zeros(1, np.float32),
            "orig_im_size": np.array(self.input_size, np.float32)})
        masks = S.transform_masks(masks, self.original_size, np.linalg.inv(self.T))
        return (masks[0].max(axis=0) > 0)


class PartRecolorer:
    """Click-to-select recolor for items with several colours (flower: pink bloom + green leaves).
      mask(points)  -> soft mask of the clicked part (Segment Anything, refined with colour)
      render(parts) -> image with each part recoloured
    points = [(x, y, 1 or 0)] in image pixels (1 = part of it, 0 = not part of it)
    parts  = [{"points": points, "hex": "#RRGGBB", "grow": 4, "tol": 1.0}, ...]
    If Segment Anything is not available it falls back to a colour-based selection.
    """

    def __init__(self, path, max_side=2000):
        img = Image.open(path)
        self.icc = img.info.get("icc_profile")
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side))
        self.img = img
        self.rgb = np.asarray(img).astype(np.float32) / 255.0
        self.h, self.w = self.rgb.shape[:2]
        hsv = cv2.cvtColor(self.rgb, cv2.COLOR_RGB2HSV)
        self.H, self.S, self.V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        self._sam = None
        self.sam_error = None
        self._cache = {}
        self._regions = {}   # auto-found parts: rid -> bool mask
        self._holes = {}     # rid -> small gaps inside the part (sunlit highlights on yarn) that must be coloured too
        self._auto = None
        self._alpha = None
        self._exact = set()  # rids drawn by hand (corner clicks): the edge is exactly the drawn line

    def _clamp(self, points):
        return [(int(np.clip(x, 0, self.w - 1)), int(np.clip(y, 0, self.h - 1)), int(l)) for x, y, l in points]

    def _sam_mask(self, points):
        if self.sam_error:
            return None
        try:
            if self._sam is None:
                self._sam = SamClicker(self.img)
            return self._sam.segment(points)
        except Exception as e:  # no internet for the first download, memory, version change ...
            self.sam_error = str(e)
            return None

    def _seed_stats(self, M):
        ys, xs = np.nonzero(M)
        if len(ys) > 20000:
            sel = np.random.default_rng(0).choice(len(ys), 20000, replace=False)
            ys, xs = ys[sel], xs[sel]
        ang = np.deg2rad(self.H[ys, xs])
        Hs = float(np.rad2deg(np.arctan2(np.median(np.sin(ang)), np.median(np.cos(ang)))) % 360)
        return Hs, float(np.median(self.S[ys, xs])), float(np.median(self.V[ys, xs]))

    def _dist(self, Hs, Ss, Vs, tol):
        dH = np.abs((self.H - Hs + 180) % 360 - 180)
        wH = np.clip(np.minimum(self.S, Ss) / 0.25, 0, 1)
        return np.sqrt((dH / 18) ** 2 * wH + ((self.S - Ss) / 0.22) ** 2 + ((self.V - Vs) / 0.5) ** 2) / tol

    def add_polygon(self, pts):
        """A shape drawn by clicking its corners (like a polygon lasso). Returns its region id."""
        m = np.zeros((self.h, self.w), np.uint8)
        cv2.fillPoly(m, [np.ascontiguousarray(np.array(self._clamp([(int(x), int(y), 1) for x, y in pts]), np.int32)[:, :2])], 1)
        rid = len(self._regions) + 1
        self._regions[rid] = m.astype(bool)
        self._holes[rid] = np.zeros((self.h, self.w), bool)
        self._exact.add(rid)
        return rid

    def region_seed(self, rid):
        """A point well inside a region (for the part's anchor click)."""
        dist = cv2.distanceTransform(self._regions[rid].astype(np.uint8), cv2.DIST_L2, 5)
        y, x = np.unravel_index(dist.argmax(), dist.shape)
        return int(x), int(y)

    def overlay_poly(self, base_img, pts, closed=False):
        """Shows the corners clicked so far."""
        a = np.array(base_img.convert("RGB")).copy()
        P = np.array([[int(x), int(y)] for x, y in pts], np.int32)
        wd = max(2, int(0.003 * max(self.h, self.w)))
        if len(P) > 1:
            cv2.polylines(a, [P], closed, (255, 196, 0), wd, cv2.LINE_AA)
        for i, (x, y) in enumerate(P):
            cv2.circle(a, (int(x), int(y)), wd * 3, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(a, (int(x), int(y)), wd * 2, (255, 120, 0) if i else (0, 170, 80), -1, cv2.LINE_AA)
        return Image.fromarray(a)

    def _colour_region(self, x, y, tol):
        """Pixels of a similar colour, connected to (x, y)."""
        r = max(4, int(0.007 * max(self.h, self.w)))
        sl = (slice(max(y - r, 0), y + r + 1), slice(max(x - r, 0), x + r + 1))
        win = np.zeros((self.h, self.w), bool); win[sl] = True
        Hs, Ss, Vs = self._seed_stats(win)
        d = self._dist(Hs, Ss, Vs, tol)
        strong = cv2.morphologyEx(((1 - _smooth(d, 0.8, 1.3)) > 0.5).astype(np.uint8), cv2.MORPH_OPEN, _ellipse(5))
        n, lab = cv2.connectedComponents(strong, connectivity=8)
        near = lab[sl].ravel(); near = near[near > 0]
        return (lab == np.bincount(near).argmax()) if near.size else strong.astype(bool)

    @staticmethod
    def _small_holes(region, max_frac=0.04, compact=False):
        """Gaps inside a part: enclosed holes (a sunlit highlight, a dropped stitch) and, for COMPACT parts only
        (a bloom), shallow notches that are mostly surrounded by the part. Thin parts (leaves, stems) get enclosed holes
        only, otherwise the paper between a leaf and the stem would be filled in."""
        r8 = region.astype(np.uint8)
        lim = max_frac * float(region.sum())
        holes = np.zeros(region.shape, bool)
        cnts, hier = cv2.findContours(r8, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hier is not None:
            for c, h in zip(cnts, hier[0]):
                if h[3] != -1 and cv2.contourArea(c) <= lim:          # inner contour = a hole
                    cv2.drawContours(holes.view(np.uint8), [c], -1, 1, -1)
        if compact:
            closed = cv2.morphologyEx(r8, cv2.MORPH_CLOSE, _ellipse(31)) > 0
            n, cc, stats, _ = cv2.connectedComponentsWithStats((closed & ~region).astype(np.uint8), connectivity=8)
            for c in range(1, n):
                if stats[c, cv2.CC_STAT_AREA] > lim:
                    continue
                comp = (cc == c)
                ring = cv2.dilate(comp.astype(np.uint8), _ellipse(9)).astype(bool) & ~comp
                if ring.any() and region[ring].mean() >= 0.6:      # mostly surrounded by the part: a highlight, not the outside
                    holes |= comp
        return holes & ~region

    def auto_parts(self, k=3):
        """AUTO COLOURING. Finds the item, then splits it into its colour groups (petals / leaves, body / trim ...).
        No clicking needed. Returns [{"rid", "points", "area", "green"}, ...]; area = share of the item (0-1).
        Slow only the first time (cut-out of the item), then instant."""
        if self._auto is not None:
            return self._auto
        if self._alpha is None:
            self._alpha = _rembg_alpha(self.img)
        hard = (self._alpha > 0.5).astype(np.uint8)
        inner = cv2.erode(hard, _ellipse(5)).astype(bool)   # edge pixels are mixed with the background: leave them out
        if inner.sum() < 500:
            raise ValueError("Could not find the item in the photo. Try a photo with a clearer background.")
        lab = cv2.cvtColor(self.rgb, cv2.COLOR_RGB2LAB)
        feat = np.dstack([0.35 * lab[..., 0], lab[..., 1], lab[..., 2]])   # mostly colour, little brightness: shading does not split a petal
        X = feat[inner].astype(np.float32)
        sample = X[np.random.default_rng(0).choice(len(X), min(len(X), 30000), replace=False)]
        k = int(max(2, min(k, len(sample))))
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
        _, _, C = cv2.kmeans(sample, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
        # merge colour groups that are nearly the same
        groups = list(range(k))
        for i in range(k):
            for j in range(i + 1, k):
                if np.linalg.norm(C[i] - C[j]) < 14:
                    old, new = groups[j], groups[i]
                    groups = [new if g == old else g for g in groups]
        d = np.stack([np.linalg.norm(X - C[i], axis=1) for i in range(k)], axis=1)
        lbl = np.array(groups)[d.argmin(axis=1)]
        full = np.full((self.h, self.w), -1, np.int32)
        full[inner] = lbl
        out, total, minpix = [], int(inner.sum()), 0.0015 * self.h * self.w
        for g in sorted(set(groups)):
            m = (full == g).astype(np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _ellipse(3))
            n, cc, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
            keep = np.zeros(n, bool)
            biggest = stats[1:, cv2.CC_STAT_AREA].max() if n > 1 else 0
            for c in range(1, n):
                if stats[c, cv2.CC_STAT_AREA] >= max(minpix, 0.15 * biggest):
                    keep[c] = True
            region = keep[cc]
            region = cv2.morphologyEx(region.astype(np.uint8), cv2.MORPH_CLOSE, _ellipse(11)).astype(bool) & (hard > 0)
            if region.sum() / total < 0.04:
                continue
            ang = np.deg2rad(self.H[region])
            hue = float(np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) % 360)
            sat = float(np.median(self.S[region]))
            if sat > 0.45:   # strongly coloured yarn: also take touching yarn of the same hue that clustered elsewhere (paler tips)
                dh = np.abs((self.H - hue + 180) % 360 - 180)
                cand = (hard > 0) & (dh < 18) & (self.S > 0.7 * sat) & ~region
                n2, cc2 = cv2.connectedComponents(cand.astype(np.uint8), connectivity=8)
                near = cv2.dilate(region.astype(np.uint8), _ellipse(9)).astype(bool)
                touch = np.unique(cc2[near & cand]); touch = touch[touch > 0]
                add = np.isin(cc2, touch)
                region = region | cv2.morphologyEx(add.astype(np.uint8), cv2.MORPH_CLOSE, _ellipse(5)).astype(bool)
            cnts, _ = cv2.findContours(region.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            compact = float(region.sum()) / max(cv2.contourArea(cv2.convexHull(np.vstack(cnts))), 1.0) > 0.85
            holes = self._small_holes(region, compact=compact)
            region = region | holes
            area = float(region.sum()) / total
            cnts, _ = cv2.findContours(region.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            hull = cv2.contourArea(cv2.convexHull(np.vstack(cnts)))
            solid = float(region.sum()) / max(hull, 1.0)      # compact blob (bloom) vs scattered strips (wrapper paper)
            dist = cv2.distanceTransform(region.astype(np.uint8), cv2.DIST_L2, 5)
            y, x = np.unravel_index(dist.argmax(), dist.shape)
            rid = len(self._regions) + 1
            self._regions[rid] = region
            self._holes[rid] = holes
            out.append({"rid": rid, "points": [(int(x), int(y), 1)], "area": area, "hue": hue, "sat": sat,
                        "solid": solid, "green": bool(40 <= hue <= 170 and sat > 0.3)})
        self._auto = out
        return out

    def mask(self, points, tol=1.0, grow=4, smart=True, rid=None):
        points = self._clamp(points)
        key = (tuple(points), round(float(tol), 2), int(grow), bool(smart), rid)
        if key in self._cache:
            return self._cache[key]
        pos = [p for p in points if p[2] == 1] or points
        region = self._regions.get(rid) if rid is not None else None
        if region is not None:
            # auto-found part: later clicks add to it (+) or cut from it (-)
            M = region.copy()
            rad = max(6, int(0.02 * max(self.h, self.w)))
            for x, y, l in points[1:]:
                if l == 1:
                    M |= self._colour_region(x, y, tol)
                else:
                    cut = np.zeros(M.shape, np.uint8); cv2.circle(cut, (x, y), rad, 1, -1)
                    M &= ~cut.astype(bool)
        else:
            M = self._sam_mask(points) if smart else None
        if M is None or not M.any():
            M = self._colour_region(pos[0][0], pos[0][1], tol)
        M8 = M.astype(np.uint8)
        core8 = cv2.erode(M8, _ellipse(7))
        Hs, Ss, Vs = self._seed_stats(core8.astype(bool) if core8.any() else M)
        d = self._dist(Hs, Ss, Vs, tol)

        exact = rid in self._exact
        if exact:   # hand-drawn shape: the edge is the drawn line; only pixels of a very different colour (a pom-pom) are left out
            f = np.clip(cv2.GaussianBlur(M8.astype(np.float32), (0, 0), 0.9) * (1 - _smooth(d, 2.0, 3.0)), 0, 1)
            # outer edge: dark edge pixels are a blend of yarn and background, so take them by colour. Never across a seam
            # shared with another drawn shape, so the cut between two same-colour parts stays exactly where it was drawn.
            others = np.zeros(M.shape, bool)
            for o in self._exact:
                if o != rid:
                    others |= self._regions[o]
            others_f = cv2.GaussianBlur(cv2.dilate(others.astype(np.uint8), _ellipse(5)).astype(np.float32), (0, 0), 1.0)
            gate = cv2.GaussianBlur(cv2.dilate(M8, _ellipse(max(int(grow) * 2 + 1, 9))).astype(np.float32), (0, 0), 1.0) * (1 - others_f)
            band = (1 - _smooth(d, 0.8, 1.8)) * gate
            if Ss > 0.45:
                ring = (cv2.dilate(M8, _ellipse(41)) - cv2.dilate(M8, _ellipse(15))).astype(bool) & ~others
                sO = float(np.median(self.S[ring])) if ring.sum() > 50 else 0.08
                dHs = np.abs((self.H - Hs + 180) % 360 - 180)
                f_sat = np.clip((self.S - sO) / (max(Ss - sO, 0.05) / tol), 0, 1) * (1 - _smooth(dHs, 25, 55))
                band = np.maximum(band, f_sat * gate)
            f = np.clip(np.maximum(f, band), 0, 1)
        else:
            # inside: keep everything except pixels of a very different colour (e.g. a white pom-pom inside a red cut-out)
            inside = cv2.GaussianBlur(cv2.erode(M8, _ellipse(3)).astype(np.float32), (0, 0), 1.0) * (1 - _smooth(d, 2.0, 3.0))
            # edge band: decide by colour, so no old-colour outline stays and no neighbouring colour is taken
            gate = cv2.GaussianBlur(cv2.dilate(M8, _ellipse(max(int(grow), 1))).astype(np.float32), (0, 0), 1.0)
            band = (1 - _smooth(d, 0.8, 1.8)) * gate
            if Ss > 0.45:  # strongly coloured part: estimate the old-colour share of edge pixels from saturation (cleaner edges)
                ring = (cv2.dilate(M8, _ellipse(41)) - cv2.dilate(M8, _ellipse(15))).astype(bool)
                sO = float(np.median(self.S[ring])) if ring.any() else 0.08
                dHs = np.abs((self.H - Hs + 180) % 360 - 180)
                f_sat = np.clip((self.S - sO) / (max(Ss - sO, 0.05) / tol), 0, 1) * (1 - _smooth(dHs, 25, 55))
                gate_sat = cv2.GaussianBlur(cv2.dilate(M8, _ellipse(max(int(grow) * 2 + 1, 9))).astype(np.float32), (0, 0), 1.0)
                band = np.maximum(band, f_sat * gate_sat)
            f = np.clip(np.maximum(inside, band), 0, 1)
            hole = self._holes.get(rid) if rid is not None else None
            if hole is not None and hole.any():     # sunlit highlights are pale, so the colour test would skip them
                f = np.maximum(f, cv2.GaussianBlur(hole.astype(np.float32), (0, 0), 1.0))

        other = (f < 0.05).astype(np.float32)  # colour of whatever is next to the part, measured locally
        num = cv2.GaussianBlur(self.rgb * other[..., None], (0, 0), 8)
        den = cv2.GaussianBlur(other, (0, 0), 8)
        O = np.where((den > 0.05)[..., None], num / np.maximum(den, 1e-3)[..., None], 0.9)
        if not exact:
            f = np.maximum(f, _smooth(f, 0.12, 0.5))
        out = {"f": f.astype(np.float32), "O": O.astype(np.float32)}
        if len(self._cache) > 24:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out

    def render(self, parts, natural=False):
        out = self.rgb.copy()
        claimed = np.zeros((self.h, self.w), np.float32)
        for p in parts:
            m = self.mask(p["points"], p.get("tol", 1.0), p.get("grow", 4), p.get("smart", True), p.get("rid"))
            f = _soften(np.clip(m["f"] * (1 - claimed), 0, 1))
            f3 = f[..., None]
            G = np.clip((self.rgb - (1 - f3) * m["O"]) / np.maximum(f3, 0.1), 0, 1)
            lumG = G.max(axis=2)
            shading = _shade(p["hex"], lumG, f > 0.9, f, natural)
            Gp = _paint(p["hex"], shading, natural)
            out = np.clip(out + f3 * (Gp - G), 0, 1)
            claimed = np.maximum(claimed, f)
        return Image.fromarray((out * 255).astype(np.uint8))

    def areas(self, parts):
        """Pixels covered by each coloured part (overlaps counted once, first part wins), like a shape-area readout."""
        claimed = np.zeros((self.h, self.w), np.float32)
        out = []
        for p in parts:
            m = self.mask(p["points"], p.get("tol", 1.0), p.get("grow", 4), p.get("smart", True), p.get("rid"))
            f = np.clip(m["f"] * (1 - claimed), 0, 1)
            out.append(int((f > 0.5).sum()))
            claimed = np.maximum(claimed, f)
        return out

    def item_area(self):
        """Pixels of the whole item (cut-out), for turning part areas into shares."""
        if self._alpha is None:
            self._alpha = _rembg_alpha(self.img)
        return int((self._alpha > 0.5).sum())

    def owner(self, point, parts):
        """Index of an already-recoloured part under this click, or None."""
        x, y, _ = self._clamp([point])[0]
        for i, p in enumerate(parts):
            if self.mask(p["points"], p.get("tol", 1.0), p.get("grow", 4), p.get("smart", True), p.get("rid"))["f"][y, x] > 0.5:
                return i
        return None

    def overlay(self, base_img, points, tol=1.0, grow=4, smart=True, rid=None):
        """Shows what is selected: yellow tint, outline and the clicked points."""
        m = self.mask(points, tol, grow, smart, rid)
        base = np.asarray(base_img).astype(np.float32) / 255.0
        f = m["f"][..., None]
        out = base * (1 - 0.45 * f) + np.array([1.0, 0.85, 0.1], np.float32) * 0.45 * f
        edge = cv2.morphologyEx((m["f"] > 0.5).astype(np.uint8), cv2.MORPH_GRADIENT, _ellipse(3)).astype(bool)
        out[edge] = [0.1, 0.1, 0.1]
        rad = max(4, int(0.006 * self.w))
        for x, y, l in self._clamp(points):
            col = (0.1, 0.8, 0.2) if l == 1 else (0.9, 0.1, 0.1)
            cv2.circle(out, (x, y), rad, col, -1)
            cv2.circle(out, (x, y), rad, (1, 1, 1), 2)
        return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))

    def save(self, img, path):
        if self.icc:
            img.save(path, icc_profile=self.icc)
        else:
            img.save(path)