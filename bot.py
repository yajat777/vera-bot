import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Vera Bot")
START_TIME = time.time()

CONTEXT_STORE: Dict[str, Dict[str, Any]] = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
SEED_FALLBACK: Dict[str, Dict[str, Any]] = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}

def _load_seeds():
    for p in Path(".").rglob("customers_seed.json"):
        d = p.parent
        try:
            for f in (d / "categories").glob("*.json"):
                c = json.loads(f.read_text())
                if c.get("slug"): SEED_FALLBACK["category"][c["slug"]] = c
            for fname, scope, key in [("merchants_seed.json","merchant","merchant_id"),("customers_seed.json","customer","customer_id"),("triggers_seed.json","trigger","id")]:
                fpath = d / fname
                if fpath.exists():
                    raw = json.loads(fpath.read_text())
                    for item in raw.get(scope+"s", raw.get(scope, [])):
                        if key in item: SEED_FALLBACK[scope][item[key]] = item
        except Exception: pass
        break
_load_seeds()

ENDED_CONVS: Dict[str, int] = {}
OPTED_OUT: Set[str] = set()
AUTO_TURNS: Dict[str, Set[int]] = {}
CONV_CTX: Dict[str, Dict[str, Any]] = {}

LEX = {
    "dentists": {"biz": "practice", "peers": "practices", "cust": "patient", "custs": "patients", "unit": "evening chairs", "chk": "clinical"},
    "salons": {"biz": "salon", "peers": "salons", "cust": "client", "custs": "clients", "unit": "styling chairs", "chk": "salon"},
    "restaurants": {"biz": "outlet", "peers": "outlets", "cust": "diner", "custs": "diners", "unit": "peak-hour orders", "chk": "kitchen"},
    "gyms": {"biz": "studio", "peers": "fitness clubs", "cust": "member", "custs": "members", "unit": "batch slots", "chk": "coaching"},
    "pharmacies": {"biz": "pharmacy", "peers": "pharmacies", "cust": "patient", "custs": "patients", "unit": "prescription orders", "chk": "dispensary"}
}

def now_iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def clean(t): return re.sub(r"\s+", " ", str(t or "").replace("_", " ").replace(":", " ")).strip()

def h_date(s, inc_yr=True):
    if not s: return ""
    st = str(s).strip()
    try:
        if "T" in st:
            dt = datetime.fromisoformat(st)
            if dt.hour == 0 and dt.minute == 0:
                return dt.strftime("%-d %b %Y" if inc_yr else "%-d %b")
            return dt.strftime("%-d %b %Y, %-I:%M %p" if inc_yr else "%-d %b, %-I:%M %p")
        dt = datetime.strptime(st[:10], "%Y-%m-%d")
        return dt.strftime("%-d %b %Y" if inc_yr else "%-d %b")
    except Exception: return st

def h_item(item_id, category):
    if not item_id: return "latest industry circular"
    for d in category.get("digest") or []:
        if d.get("id") == item_id:
            src = d.get("source") or ""
            title = d.get("title") or ""
            m = re.match(r"^d_(\d{4})W(\d+)_(.+)$", str(item_id))
            if m:
                w = [x.upper() if len(x)<=4 else x.capitalize() for x in m.group(3).replace("_", " ").split()]
                return f"{w[0]} Week {m.group(2)} {' '.join(w[1:])} Circular" if len(w)>=2 else f"Week {m.group(2)} {' '.join(w)} Circular"
            if src or title: return f"{src} {title}".strip()
    m = re.match(r"^d_(\d{4})W(\d+)_(.+)$", str(item_id))
    if m:
        w = [x.upper() if len(x)<=4 else x.capitalize() for x in m.group(3).replace("_", " ").split()]
        return f"{w[0]} Week {m.group(2)} {' '.join(w[1:])} Circular" if len(w)>=2 else f"Week {m.group(2)} {' '.join(w)} Circular"
    return clean(item_id)

def get_offer(m):
    for o in (m.get("offers") or []):
        if o.get("status", "active") == "active" and o.get("title"): return str(o["title"])
    for o in (m.get("offers") or []):
        if o.get("title"): return str(o["title"])
    return ""

def get_cname(cust, cid):
    if cust:
        i = cust.get("identity") or {}
        fn = str(i.get("first_name") or i.get("name") or "").strip()
        if fn:
            parts = fn.split()
            if parts[0].lower().rstrip(".") in ("mr", "mrs", "ms", "dr", "shri", "smt") and len(parts) > 1:
                return f"{parts[0]} {parts[1]}"
            return parts[0]
    if cid and "_" in str(cid):
        p = str(cid).split("_")
        if len(p)>=3 and p[2].isalpha(): return p[2].capitalize()
    return "your customer"

def describe_signals(signals, lx):
    for s in (signals or []):
        st = str(s)
        if "stale_posts:" in st:
            d = st.split(":", 1)[1].replace("d", "").strip()
            return f"after {d} days without a profile update"
        if "dormant_with_vera_" in st:
            d = st.rsplit("_", 1)[-1].replace("d", "").strip()
            return f"after {d} days without an active campaign"
        if "renewal_due_soon:" in st:
            d = st.split(":", 1)[1].replace("d", "").strip()
            return f"with {d} days left on your current plan"
    if "high_engagement" in (signals or []):
        return f"with strong weekly {lx['cust']} engagement"
    return f"across local {lx['cust']} searches"

def synthesize_payload(tp, category):
    facts = []
    skip = {"category", "category_relevance", "is_weeknight", "is_imminent", "is_expected_seasonal", "shelf_action_recommended", "delivery_address_saved", "verified", "last_ask_at"}
    for k, v in tp.items():
        if v is None or k in skip or isinstance(v, bool): continue
        kl = str(k).lower()
        if kl in ("top_item_id", "alert_id", "digest_item_id", "item_id"):
            facts.append(h_item(v, category))
        elif "date" in kl or "iso" in kl:
            lbl = clean(re.sub(r"(_iso|_date)$", "", kl))
            facts.append(f"{lbl} on {h_date(v, False)}")
        elif isinstance(v, float) and -1.0 <= v <= 1.0:
            lbl = clean(re.sub(r"(_pct|_rate)$", "", kl))
            facts.append(f"{int(round(v * 100))}% {lbl}")
        elif isinstance(v, (int, float)) and any(w in kl for w in ("amount", "price", "cost", "fee")):
            facts.append(f"₹{v} {clean(kl)}")
        elif isinstance(v, list) and v:
            items = [str(x.get("label", x) if isinstance(x, dict) else clean(x).replace("+", "up ").replace("-", "down ")) for x in v[:3]]
            facts.append(f"{clean(kl)}: {', '.join(items)}")
        elif "quote" in kl or "message" in kl:
            facts.append(f"noting '{v}'")
        else:
            facts.append(f"{clean(kl)} of {clean(v)}")
    return ", ".join(facts[:3])
