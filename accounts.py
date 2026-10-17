"""Optional sign-in. Each account gets its own folder (orders, prices, logo), so nobody sees anybody else's data.

On your laptop nothing changes: no sign-in, files stay next to app.py.
On a server set  KARSAZ_ACCOUNTS=1  (and KARSAZ_DATA=/path/to/disk) to switch accounts on.
Guests (not signed in) can still make quotes, colour previews and quote cards, but nothing is saved for them.
"""
import copy, hashlib, hmac, json, os, re, secrets, shutil, threading, time
from quote import HERE, CFG as DEFAULT_CFG, CFG_PATH

ACCOUNTS_ON = os.environ.get("KARSAZ_ACCOUNTS", "").strip().lower() in ("1", "true", "yes", "on")
DATA_DIR = os.environ.get("KARSAZ_DATA") or os.path.join(HERE, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
DEFAULT_SHOP = "KARSAZ Stitch Studio"
_lock = threading.Lock()
_fails = {}          # username -> [timestamps of recent wrong passwords]

# the prices a user may edit: (config path, label, minimum, maximum)
SETTING_FIELDS = [
    (("yarn_cost_per_100g",), "Yarn cost per 100 g (Rs)", 1, 100000),
    (("hourly_rate",), "Your hourly rate (Rs)", 0, 100000),
    (("extra_color_charge",), "Charge for each extra colour (Rs)", 0, 100000),
    (("default_markup",), "Markup (1.4 = add 40%)", 1, 20),
    (("round_to",), "Round prices to the nearest (Rs)", 1, 10000),
    (("schedule", "hours_per_day"), "Hours you crochet per day", 0.5, 24),
    (("schedule", "buffer_days"), "Spare days added to every date", 0, 60),
]


class Store:
    """Where one person's files live."""
    def __init__(self, base, user=None, cfg_path=None):
        self.base, self.user = base, user
        self.orders_path = os.path.join(base, "orders.xlsx")
        self.cfg_path = cfg_path or os.path.join(base, "pricing_config.json")

    def cfg(self):
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return copy.deepcopy(DEFAULT_CFG)

    def shop_name(self):
        return DEFAULT_SHOP        # the brand name is fixed for everyone


def local_store():
    return Store(HERE, None, CFG_PATH)


def store_for(acct):
    """acct = the per-session dict {'user': name or None}. None means guest (nothing is saved)."""
    if not ACCOUNTS_ON:
        return local_store()
    user = (acct or {}).get("user")
    return Store(os.path.join(DATA_DIR, "users", user), user) if user else None


# ---------- sign up / sign in ----------
def _users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def clean_username(u):
    return (u or "").strip().lower()


def sign_up(username, password):
    u = clean_username(username)
    if not re.fullmatch(r"[a-z0-9_]{3,20}", u):
        return None, "Pick a username of 3 to 20 letters, numbers or _ (no spaces)."
    if len(password or "") < 6:
        return None, "Use a password of at least 6 characters."
    with _lock:
        users = _users()
        if u in users:
            return None, "That username is taken. Try another one."
        salt = secrets.token_hex(16)
        users[u] = {"salt": salt, "hash": _hash(password, salt), "created": time.strftime("%Y-%m-%d")}
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(users, f)
        os.replace(tmp, USERS_FILE)
    base = os.path.join(DATA_DIR, "users", u)
    os.makedirs(base, exist_ok=True)
    cfg = copy.deepcopy(DEFAULT_CFG)          # start from the shop's default prices; the user edits their own copy
    with open(os.path.join(base, "pricing_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return u, f"Account created. Welcome, {u}!"


def sign_in(username, password):
    u = clean_username(username)
    now = time.time()
    recent = [t for t in _fails.get(u, []) if now - t < 300]
    if len(recent) >= 5:
        return None, "Too many wrong tries. Wait a few minutes and try again."
    rec = _users().get(u)
    if rec and hmac.compare_digest(_hash(password or "", rec["salt"]), rec["hash"]):
        _fails.pop(u, None)
        return u, f"Signed in as {u}."
    _fails[u] = recent + [now]
    return None, "Username or password is not right."


# ---------- price settings ----------
def get_settings(store):
    cfg = store.cfg()
    vals = []
    for path, *_ in SETTING_FIELDS:
        v = cfg
        for k in path:
            v = v[k]
        vals.append(v)
    return vals


def save_settings(store, values):
    cfg = store.cfg()
    for (path, label, lo, hi), v in zip(SETTING_FIELDS, values):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return f"'{label}' needs a number."
        if not (lo <= v <= hi):
            return f"'{label}' should be between {lo:g} and {hi:g}."
        if v == int(v) and path[-1] not in ("default_markup",):
            v = int(v)
        d = cfg
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = v
    os.makedirs(store.base, exist_ok=True)
    tmp = store.cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, store.cfg_path)
    return "Saved. New quotes use these prices."


def reset_settings(store):
    """Back to the shop defaults (only the editable numbers; the size charts stay as they are)."""
    cfg = store.cfg()
    for path, *_ in SETTING_FIELDS:
        src, dst = DEFAULT_CFG, cfg
        for k in path[:-1]:
            src, dst = src[k], dst[k]
        dst[path[-1]] = src[path[-1]]
    with open(store.cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)