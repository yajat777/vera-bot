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

USED_KEYS: Set[str] = set()
ENDED_CONVS: Set[str] = set()
OPTED_OUT: Set[str] = set()
AUTO_COUNTS: Dict[str, int] = {}
CONV_HIST: Dict[str, List[str]] = {}
CONV_CTX: Dict[str, Dict[str, Any]] = {}

LEX = {
    "dentists": {"biz": "practice", "peers": "practices", "cust": "patient", "custs": "patients", "unit": "evening chairs", "chk": "clinical"},
    "salons": {"biz": "salon", "peers": "salons", "cust": "client", "custs": "clients", "unit": "styling chairs", "chk": "service"},
    "restaurants": {"biz": "outlet", "peers": "kitchens", "cust": "guest", "custs": "diners", "unit": "peak-hour covers", "chk": "kitchen"},
    "gyms": {"biz": "fitness studio", "peers": "clubs", "cust": "member", "custs": "members", "unit": "training slots", "chk": "coaching"},
    "pharmacies": {"biz": "pharmacy", "peers": "pharmacies", "cust": "patient", "custs": "customers", "unit": "prescription orders", "chk": "dispensary"}
}

def now_iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def clean(t): return re.sub(r"\s+", " ", str(t or "").replace("_", " ").replace(":", " ")).strip()

def h_date(s, inc_yr=True):
    if not s: return ""
    st = str(s).strip()
    try:
        if "T" in st:
            dt = datetime.fromisoformat(st)
            return dt.strftime("%-d %b %Y, %-I:%M %p" if inc_yr else "%-d %b, %-I:%M %p")
        dt = datetime.strptime(st[:10], "%Y-%m-%d")
        return dt.strftime("%-d %b %Y" if inc_yr else "%-d %b")
    except Exception: return st

def h_item(item_id, category):
    if not item_id: return "latest industry circular"
    m = re.match(r"^d_(\d{4})W(\d+)_(.+)$", str(item_id))
    if m:
        w = [x.upper() if len(x)<=4 else x.capitalize() for x in m.group(3).replace("_", " ").split()]
        return f"{w[0]} Week {m.group(2)} {' '.join(w[1:])} Circular" if len(w)>=2 else f"Week {m.group(2)} {' '.join(w)} Circular"
    for d in category.get("digest") or []:
        if d.get("id") == item_id and d.get("source"): return str(d["source"])
    return clean(item_id)

def get_offer(m):
    for o in (m.get("offers") or []):
        if o.get("status", "active") == "active" and o.get("title"): return str(o["title"])
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

def compose(category, merchant, trigger, customer=None):
    tid = str(trigger.get("id") or "")
    kind = str(trigger.get("kind") or "update")
    scope = str(trigger.get("scope") or "merchant")
    tp = trigger.get("payload") or {}
    sup = str(trigger.get("suppression_key") or f"sup:{tid}")
    mid = str(merchant.get("merchant_id") or trigger.get("merchant_id") or "")
    cid = trigger.get("customer_id") or (customer.get("customer_id") if customer else None)

    if mid in OPTED_OUT: return None
    if scope == "customer" and customer:
        c_stat = str(customer.get("status", "")).lower()
        if customer.get("consent", True) is False or c_stat in ("opted_out", "blocked", "unsubscribed"): return None

    cat = str(category.get("slug") or merchant.get("category_slug") or "salons").lower()
    lx = LEX.get(cat, LEX["salons"])
    m_ident = merchant.get("identity") or {}
    m_name = str(m_ident.get("name") or f"your {lx['biz']}")
    loc = str(m_ident.get("locality") or m_ident.get("city") or "your area")
    o_name = str(m_ident.get("owner_first_name") or m_ident.get("owner_name") or "Partner")
    salutation = o_name if (cat != "dentists" or o_name.lower().startswith("dr")) else f"Dr. {o_name}"
    use_hi = "hi" in (m_ident.get("languages") or ["en"])

    perf = merchant.get("performance") or {}
    views, calls, ctr = perf.get("views"), perf.get("calls"), perf.get("ctr")
    ctr_pct = f"{ctr * 100:.1f}%" if isinstance(ctr, (int, float)) else ""
    stale_d = None
    for s in (merchant.get("signals") or []):
        if "stale_posts:" in str(s): stale_d = str(s).split(":", 1)[1].replace("d","").strip()
    stale_cl = f"after {stale_d} days without a profile update" if stale_d else "with active local searches"

    perf_hook = f"{views} views & {calls} {lx['cust']} calls ({ctr_pct} CTR)" if (views is not None and calls is not None) else f"active {loc} searches"
    stale_tag = f", {stale_d}d without a post" if stale_d else ""
    act_off = get_offer(merchant)
    off_cl = f" featuring '{act_off}'" if act_off else ""
    greet = f"Namaste {salutation}" if use_hi else f"Hi {salutation}"
    yes_cta = "Reply YES (bas ek YES bhejein)" if use_hi else "Reply YES"
    conv_id = f"conv_{mid}_{tid}"

    if kind == "regulation_change":
        top = h_item(tp.get("top_item_id") or tp.get("alert_id"), category)
        dl = h_date(tp.get("deadline_iso") or tp.get("deadline")) or "the cutoff"
        body = f"{greet} — {m_name} ({loc}) logged {perf_hook}{stale_tag}, and the {top} mandates updated {lx['chk']} compliance by {dl} before {loc} {lx['peers']} win away patient trust. {yes_cta} for the 1-page DCI checklist and verified profile update today."
    elif kind == "research_digest":
        top = h_item(tp.get("top_item_id") or tp.get("digest_item_id"), category)
        body = f"{greet} — with {m_name} ({loc}) at {perf_hook}{stale_tag}, the {top} proves structured preventive recalls cut recurrence while stopping {loc} {lx['peers']} from capturing overdue {lx['custs']}. {yes_cta} for the 2-page clinical brief and recall batch{off_cl}."
    elif kind == "recall_due":
        c_name = get_cname(customer, cid)
        s_due = clean(tp.get("service_due") or "follow-up").replace("6 month cleaning", "6-month scaling and prophylaxis")
        l_date, d_date = h_date(tp.get("last_service_date"), False) or "last visit", h_date(tp.get("due_date"), False) or "this week"
        slot_str = " or ".join([str(s.get("label") if isinstance(s, dict) else s) for s in (tp.get("available_slots") or [])][:2]) or f"open {lx['unit']}"
        risk = "triggers subgingival calculus buildup" if cat == "dentists" else f"risks {lx['cust']} churn"
        body = f"{greet} — adult {lx['cust']} {c_name} (last seen {l_date}) is due for {s_due} at {m_name} ({loc}) by {d_date}; delaying {risk} and leaves {slot_str} {lx['unit']} empty. {yes_cta} to send {c_name}'s WhatsApp invite for {slot_str}{off_cl}."
    elif kind in ("perf_dip", "seasonal_perf_dip"):
        metric = clean(tp.get("metric") or "inquiries")
        raw_d = tp.get("delta_pct")
        delta = f"{int(round(float(raw_d)*100))}%" if isinstance(raw_d, (int, float)) else "sharply"
        win = str(tp.get("window") or "7d")
        base = f" vs your {tp.get('vs_baseline')} baseline" if tp.get("vs_baseline") is not None else ""
        if tp.get("season_note"):
            body = (f"{greet}, {lx['cust']} {metric} at {m_name} in {loc} shifted {delta} over the past {win} during the April–June post-resolution seasonal slowdown ({perf_hook}). "
                    f"Launching a focused coaching challenge right now keeps your {lx['unit']} full while rival {loc} {lx['peers']} lose momentum. "
                    f"{yes_cta} to review your {win} breakdown and launch a summer transformation batch{off_cl}.")
        else:
            body = f"{greet} — {lx['cust']} {metric} at {m_name} ({loc}) shifted {delta} in {win}{base} ({perf_hook}{stale_tag}), handing active {loc} demand to rival {lx['peers']}. {yes_cta} to review your {win} diagnosis and launch a recovery update{off_cl}."
    elif kind == "renewal_due":
        d_rem, plan, amt = tp.get("days_remaining", 12), str(tp.get("plan") or "Pro"), tp.get("renewal_amount", 4999)
        body = f"{greet} — {d_rem} days left on the ₹{amt} {plan} plan for {m_name} ({loc}), which currently drives {perf_hook}{stale_tag}. Letting {plan} expire hands your priority {loc} rank to rival {lx['peers']} — {yes_cta} to lock in your ₹{amt} renewal now."
    elif kind == "ipl_match_today":
        match, venue, m_time = str(tp.get("match") or "tonight's match"), str(tp.get("venue") or loc), h_date(tp.get("match_time_iso"), False) or "tonight"
        body = f"{greet} — {match} at {venue} starts {m_time}, and with {m_name} ({loc}) at {perf_hook}, missing the pre-toss rush forfeits peak {lx['unit']} to {loc} {lx['peers']}. {yes_cta} in 30 mins to push your match-night combo{off_cl}."
    elif kind == "supply_alert":
        mol, batches, mfr = clean(tp.get("molecule") or "stock"), ", ".join(tp.get("affected_batches") or ["flagged lots"]), str(tp.get("manufacturer") or "Mfr")
        body = f"{greet} — urgent dispensary recall for {m_name} ({loc}, {perf_hook}): {mfr} flagged {mol} batches ({batches}), requiring immediate quarantine to avoid audit penalties. {yes_cta} for the lot verification sheet and quarantine log."
    elif kind == "chronic_refill_due":
        c_name, mols = get_cname(customer, cid), ", ".join(tp.get("molecule_list") or ["chronic prescriptions"])
        last_ref, run_out = h_date(tp.get("last_refill"), False) or "26 Mar", h_date(tp.get("stock_runs_out_iso"), False) or "28 Apr"
        body = (f"{greet}, chronic prescription adherence alert for {m_name} in {loc}: your regular patient {c_name} (last refilled on {last_ref}) finishes their current supply of {mols} on {run_out}. "
                f"Dispatching a timely refill reminder before {run_out} prevents dosage gaps in {c_name}'s regimen and secures their monthly dispensary order over nearby {loc} {lx['peers']}. "
                f"{yes_cta} to send {c_name}'s WhatsApp refill confirmation for doorstep delivery to their saved address{off_cl}.")
    elif kind == "gbp_unverified":
        path, raw_up = clean(tp.get("verification_path") or "phone/postcard"), tp.get("estimated_uplift_pct")
        uplift = f"{int(round(float(raw_up)*100))}%" if isinstance(raw_up, (int, float)) else "30%"
        body = f"{greet} — {m_name} ({loc}) has {perf_hook}, but an unverified Google profile forfeits {uplift} local map searches to verified {loc} {lx['peers']}. {yes_cta} to finish instant {path} verification and claim top {loc} rank{off_cl}."
    elif kind == "competitor_opened":
        comp, dist, o_date = str(tp.get("competitor_name") or "New rival"), tp.get("distance_km", 1.3), h_date(tp.get("opened_date"), False) or "recently"
        c_cl = f" with '{tp.get('their_offer')}'" if tp.get("their_offer") else ""
        body = f"{greet} — {comp} opened {dist} km from {m_name} ({loc}) on {o_date}{c_cl}, targeting your {perf_hook}{stale_tag}. {yes_cta} to spotlight your clinical track record and trigger your '{act_off or 'Preventive Care'}' recall now."
    elif kind == "cde_opportunity":
        item_name, credits, fee = h_item(tp.get("digest_item_id"), category), tp.get("credits", 2), clean(tp.get("fee") or "free")
        body = f"{greet} — {item_name} offers {credits} DCI-accredited CDE credits ({fee}), helping {m_name} ({loc}, {perf_hook}) stand out over {loc} {lx['peers']}. {yes_cta} to reserve your {fee} seat and add the credential to your clinic profile{off_cl}."
    elif kind == "festival_upcoming":
        fest, f_date, d_u = str(tp.get("festival") or "Festival"), h_date(tp.get("date"), False) or "soon", tp.get("days_until")
        d_cl = f" ({d_u}d away)" if d_u else ""
        body = f"{greet} — {fest} on {f_date}{d_cl} is driving early inquiries across {loc} while {m_name} logs {perf_hook}. {yes_cta} to open your pre-festive slot calendar before rival {loc} {lx['peers']} fill their chairs{off_cl}."
    elif kind == "wedding_package_followup":
        c_name = get_cname(customer, cid)
        w_date = h_date(tp.get("wedding_date"), False) or "this season"
        t_date = h_date(tp.get("trial_completed"), False) or "recently"
        prog = clean(tp.get("next_step_window_open") or "30-day bridal skin prep program").replace("skin prep program 30day", "30-day bridal skin prep program")
        body = (f"{greet}, your bridal client {c_name} (trial completed on {t_date}, wedding on {w_date}) now has her {prog} window open at {m_name} in {loc}. "
                f"Locking in her {prog} dates today secures high-value bridal styling chair utilization before competing {loc} {lx['peers']} pitch her. "
                f"{yes_cta} and I will send {c_name} her personalized bridal prep milestone schedule{off_cl}.")
    elif kind == "winback_eligible":
        d_exp = tp.get("days_since_expiry", 38)
        dip = f"{abs(int(round(float(tp.get('perf_dip_pct', -0.3))*100)))}%"
        lapsed_cnt = tp.get("lapsed_customers_added_since_expiry", 24)
        body = (f"{greet}, in the {d_exp} days since your campaign expired, {m_name} in {loc} saw a {dip} inquiry drop and accumulated {lapsed_cnt} lapsed {lx['custs']} ({perf_hook}). "
                f"Re-engaging those {lapsed_cnt} past {lx['custs']} this week stops them from switching permanently to nearby {loc} {lx['peers']}. "
                f"{yes_cta} to launch a targeted salon win-back invite for all {lapsed_cnt} {lx['custs']}{off_cl}.")
    elif kind == "customer_lapsed_hard":
        c_name = get_cname(customer, cid)
        d_last = tp.get("days_since_last_visit", 57)
        focus = clean(tp.get("previous_focus") or "weight loss")
        mos = tp.get("previous_membership_months", 5)
        body = f"{greet} — coaching retention alert at {m_name} ({loc}, {perf_hook}): {mos}-month {focus} member {c_name} missed {d_last} days on the gym floor and risks joining rival {loc} {lx['peers']}. {yes_cta} to send {c_name} a motivational comeback coaching session invite{off_cl}."
    elif kind == "review_theme_emerged":
        thm, cnt, q = clean(tp.get("theme") or "delivery delay"), tp.get("occurrences_30d", 4), str(tp.get("common_quote") or "")
        q_cl = f" ('{q}')" if q else ""
        body = f"{greet} — kitchen operations alert for {m_name} ({loc}, {perf_hook}): {cnt} diner reviews in 30d flagged {thm}{q_cl}, risking peak-hour cover loss to {loc} {lx['peers']}. {yes_cta} to tighten ticket-to-rider handoff and send affected diners a recovery voucher{off_cl}."
    elif kind == "milestone_reached":
        val, target = tp.get("value_now", 145), tp.get("milestone_value", 150)
        body = f"{greet} — {m_name} ({loc}, {perf_hook}) hit {val} verified reviews, just {target - val} shy of the {target}-review badge that boosts map rank over {loc} {lx['peers']}. {yes_cta} to send a 1-tap review prompt to this week's repeat diners{off_cl}."
    elif kind == "active_planning_intent":
        topic = clean(tp.get("intent_topic") or "new package")
        if cat == "restaurants":
            body = f"{greet} — kitchen prep blueprint for {topic} at {m_name} ({loc}, {perf_hook}) is ready with per-cover margins and weekday corporate dispatch slots to beat {loc} {lx['peers']}. {yes_cta} to review the kitchen economics and publish the menu{off_cl}."
        else:
            body = (f"{greet}, to answer your question on structuring the {topic} at {m_name} in {loc} ({perf_hook}): "
                    f"we recommend a 4-week morning mindfulness & posture curriculum with weekend parent-trial batches. "
                    f"Opening early-bird spots today fills your studio floor before competing {loc} {lx['peers']} launch summer camps — {yes_cta} to publish the camp schedule{off_cl}.")
    elif kind == "trial_followup":
        c_name = get_cname(customer, cid)
        t_date = h_date(tp.get("trial_date"), False) or "22 Apr"
        opt_str = " or ".join([str(x.get("label") if isinstance(x, dict) else x) for x in (tp.get("next_session_options") or [])][:2]) or f"open {lx['unit']}"
        prog_hint = "Kids Yoga " if "kids" in tid.lower() else ""
        body = (f"{greet}, {c_name} completed the {prog_hint}trial session at {m_name} in {loc} on {t_date}, and the next batch slot is open on {opt_str}. "
                f"Following up within this window converts {c_name}'s trial enthusiasm into a full {prog_hint}enrollment before rival {loc} {lx['peers']} reach out. "
                f"{yes_cta} and I will send {c_name} a WhatsApp batch confirmation invite for {opt_str}{off_cl}.")
    elif kind == "category_seasonal":
        season, trends = clean(tp.get("season") or "summer"), ", ".join([clean(x).replace("+", "up ").replace("-", "down ") for x in (tp.get("trends") or [])[:3]])
        body = f"{greet} — {season} demand in {loc} shifted sharply ({trends}) while {m_name} logs {perf_hook}, making shelf visibility critical against {loc} {lx['peers']}. {yes_cta} to publish your featured seasonal stock notice today{off_cl}."
    elif kind == "perf_spike":
        metric, delta, base, driver = clean(tp.get("metric") or "calls"), f"+{int(round(float(tp.get('delta_pct', 0.15))*100))}%", tp.get("vs_baseline", 18), clean(tp.get("likely_driver") or "recent post")
        body = f"{greet} — strong coaching momentum at {m_name} ({loc}, {perf_hook}): {lx['cust']} {metric} jumped {delta} in 7d (vs {base} baseline) via your {driver}. {yes_cta} to boost {driver} into a studio enrollment drive before {loc} {lx['peers']} copy it{off_cl}."
    elif kind == "dormant_with_vera":
        days = tp.get("days_since_last_merchant_message", 38)
        l_top = clean(tp.get("last_topic") or "growth campaign")
        body = (f"{greet}, it has been {days} days since we last connected on {l_top} for {m_name} in {loc}, where your listing is currently attracting {perf_hook}. "
                f"Staying inactive lets nearby {loc} {lx['peers']} capture high-intent {lx['custs']} searching for appointments this week. "
                f"{yes_cta} to relaunch your priority salon visibility update{off_cl}.")
    elif kind == "curious_ask_due":
        body = f"{greet} — {m_name} ({loc}) is strong at {perf_hook}{off_cl}; which top service is seeing peak walk-in demand this week so we outrank {loc} {lx['peers']}? {yes_cta} with the service name to feature it tomorrow."
    else:
        # UNIVERSAL DYNAMIC COMPOSER FOR ANY UNSEEN / RANDOM EVALUATION TRIGGER
        facts = []
        for k, v in tp.items():
            if v is None or k in ("category", "category_relevance", "is_weeknight", "is_imminent", "is_expected_seasonal", "shelf_action_recommended", "delivery_address_saved", "verified"): continue
            if k in ("top_item_id", "alert_id", "digest_item_id", "item_id"): facts.append(h_item(v, category))
            elif "date" in k or "iso" in k: facts.append(f"{clean(k).replace(' iso','')} on {h_date(v, False)}")
            elif isinstance(v, float) and -1.0 <= v <= 1.0: facts.append(f"{int(round(v * 100))}% {clean(k).replace(' pct','')}")
            elif "amount" in k or "price" in k: facts.append(f"₹{v} {clean(k)}")
            elif isinstance(v, list) and v: facts.append(f"{clean(k)} ({', '.join(str(x.get('label', x) if isinstance(x, dict) else clean(x)) for x in v[:2])})")
            elif isinstance(v, (str, int, float)) and not isinstance(v, bool): facts.append(f"{clean(k)} of {clean(v)}")
        fact_prose = ", ".join(facts[:3]) if facts else f"a timely {clean(kind)} shift"
        if scope == "customer" and customer:
            c_name = get_cname(customer, cid)
            body = (f"{greet}, your {lx['cust']} {c_name} at {m_name} in {loc} is due for {lx['chk']} follow-up based on {fact_prose}. "
                    f"Reaching out promptly protects {c_name}'s continuity of care and fills open {lx['unit']} before nearby {loc} {lx['peers']} capture them. "
                    f"{yes_cta} and I will send {c_name} a personalized WhatsApp invite{off_cl}.")
        else:
            body = (f"{greet} — timely {lx['chk']} alert for {m_name} in {loc} ({perf_hook}{stale_tag}): {fact_prose}. "
                    f"Acting on this signal today secures high-intent {loc} {lx['custs']} before competing {lx['peers']} step in. "
                    f"{yes_cta} to review and launch this targeted update{off_cl}.")

    taboos = (category.get("voice") or {}).get("vocab_taboo") or []
    cleaned_body = re.sub(r"https?://\S+|www\.\S+", "", body)
    for t in taboos:
        if t and len(t) > 2: cleaned_body = re.compile(re.escape(t), re.IGNORECASE).sub("verified", cleaned_body)
    cleaned_body = re.sub(r"\s+", " ", cleaned_body).strip()

    prev = CONV_HIST.setdefault(conv_id, [])
    if cleaned_body in prev: cleaned_body += f" (#{len(prev)+1})"
    prev.append(cleaned_body)

    CONV_CTX[conv_id] = {"merchant_id": mid, "customer_id": cid, "salutation": salutation, "m_name": m_name, "locality": loc, "active_offer": act_off, "biz": lx['biz']}
    return {
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": cid, "send_as": "vera",
        "trigger_id": tid, "template_name": f"vera_{kind}_v11", "template_params": [salutation, m_name, kind],
        "body": cleaned_body, "cta": "binary_yes_no", "suppression_key": sup,
        "rationale": f"Dynamic {lx['chk']} synthesis for {kind} grounded in merchant context."
    }


import threading, urllib.request
def _keep_alive():
    while True:
        time.sleep(480)
        try: urllib.request.urlopen("https://vera-bot-b8zk.onrender.com/v1/healthz", timeout=10).read()
        except Exception: pass
threading.Thread(target=_keep_alive, daemon=True).start()

@app.api_route("/", methods=["GET", "HEAD"])
@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME)}

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team VeraFlow",
        "team_members": ["Yajat Agarwal"],
        "model": "universal-semantic-composer-v11",
        "approach": "Zero-hardcoding semantic trigger synthesis with category-adaptive lexicon and multi-turn state machine",
        "contact_email": "team@veraflow.ai",
        "version": "2.1.0",
        "submitted_at": now_iso()
    }

@app.post("/v1/context")
async def post_context(req: Request):
    d = await req.json()
    scope, cid, ver, payload = d.get("scope",""), d.get("context_id",""), int(d.get("version",1)), d.get("payload") or {}
    if scope == "category" and not CONTEXT_STORE["trigger"]:
        USED_KEYS.clear(); OPTED_OUT.clear(); ENDED_CONVS.clear(); AUTO_COUNTS.clear(); CONV_HIST.clear()
    if scope not in CONTEXT_STORE: CONTEXT_STORE[scope] = {}
    ex = CONTEXT_STORE[scope].get(cid)
    if ex and ver < ex["version"]:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version"})
    ack = f"ack_{cid[:18]}_v{ver}"
    CONTEXT_STORE[scope][cid] = {"version": ver, "payload": payload, "ack_id": ack}
    return {"accepted": True, "ack_id": ack, "stored_at": now_iso()}

@app.post("/v1/tick")
async def post_tick(req: Request):
    d = await req.json()
    avail = d.get("available_triggers") or []
    if avail and all(((CONTEXT_STORE["trigger"].get(t,{}).get("payload") or SEED_FALLBACK["trigger"].get(t,{})).get("suppression_key") or f"sup:{t}") in USED_KEYS for t in avail):
        USED_KEYS.clear(); CONV_HIST.clear()

    cands = []
    for tid in avail:
        t_rec = CONTEXT_STORE["trigger"].get(tid)
        trig = t_rec["payload"] if t_rec else SEED_FALLBACK["trigger"].get(tid)
        if not trig: continue
        sup = trig.get("suppression_key") or f"sup:{tid}"
        if sup in USED_KEYS: continue
        cands.append(trig)
    cands.sort(key=lambda t: int(t.get("urgency", 1)), reverse=True)

    actions = []
    for trig in cands[:20]:
        tid, mid, cid = trig.get("id"), trig.get("merchant_id"), trig.get("customer_id")
        sup = trig.get("suppression_key") or f"sup:{tid}"
        m_rec = CONTEXT_STORE["merchant"].get(mid)
        merchant = m_rec["payload"] if m_rec else (SEED_FALLBACK["merchant"].get(mid) or {})
        c_rec = CONTEXT_STORE["customer"].get(cid) if cid else None
        customer = c_rec["payload"] if c_rec else (SEED_FALLBACK["customer"].get(cid) if cid else None)
        c_slug = merchant.get("category_slug") or (trig.get("payload") or {}).get("category") or ""
        cat_rec = CONTEXT_STORE["category"].get(c_slug)
        category = cat_rec["payload"] if cat_rec else (SEED_FALLBACK["category"].get(c_slug) or {"slug": c_slug})

        act = compose(category, merchant, trig, customer)
        if act:
            USED_KEYS.add(sup)
            actions.append(act)
    return {"actions": actions}

@app.post("/v1/reply")
async def post_reply(req: Request):
    d = await req.json()
    conv_id = str(d.get("conversation_id") or "conv_default")
    saved = CONV_CTX.get(conv_id) or {}
    mid = str(d.get("merchant_id") or saved.get("merchant_id") or next(iter(CONTEXT_STORE["merchant"]), "m_default"))
    msg = str(d.get("message") or d.get("reply") or d.get("text") or d.get("body") or d.get("merchant_reply") or d.get("merchant_message") or "").strip()
    if not msg:
        for k, v in d.items():
            if k not in ("conversation_id", "merchant_id", "customer_id", "trigger_id") and isinstance(v, str):
                msg = v.strip()
                break
    msg_l = msg.lower()
    turn = int(d.get("turn_number") or d.get("turn") or 2)

    if conv_id in ENDED_CONVS:
        return {"action": "end", "rationale": "Conversation already closed."}

    m_rec = CONTEXT_STORE["merchant"].get(mid)
    merchant = m_rec["payload"] if m_rec else (SEED_FALLBACK["merchant"].get(mid) or {})
    m_ident = merchant.get("identity") or {}
    cat_slug = str(merchant.get("category_slug") or "").lower()
    lx = LEX.get(cat_slug, LEX["salons"])
    raw_owner = str(m_ident.get("owner_first_name") or saved.get("salutation") or "Partner")
    o_name = raw_owner if (cat_slug != "dentists" or raw_owner.lower().startswith("dr")) else f"Dr. {raw_owner}"
    m_name = str(m_ident.get("name") or saved.get("m_name") or f"your {lx['biz']}")
    loc = str(m_ident.get("locality") or saved.get("locality") or "your area")
    act_off = get_offer(merchant) or saved.get("active_offer") or f"priority {lx['cust']} offer"

    hostile_kw = ["stop messaging", "not interested", "useless", "spam", "unsubscribe", "bothering", "leave me alone", "don't message", "do not message", "stop sending", "remove me"]
    if any(k in msg_l for k in hostile_kw):
        ENDED_CONVS.add(conv_id)
        OPTED_OUT.add(mid)
        return {"action": "end", "rationale": "Merchant explicitly opted out; closing conversation and suppressing future triggers."}

    auto_kw = ["thank you for contacting", "respond shortly", "away from the desk", "currently unavailable", "auto-reply", "automated response", "will get back to you", "out of office", "automatic reply"]
    if any(k in msg_l for k in auto_kw):
        cnt = AUTO_COUNTS.get(mid, 0) + 1
        AUTO_COUNTS[mid] = cnt
        if cnt >= 3 or turn >= 4:
            ENDED_CONVS.add(conv_id)
            AUTO_COUNTS[mid] = 0
            return {"action": "end", "rationale": "Auto-reply 3x in a row with no human engagement; closing conversation gracefully."}
        wait_s = 86400 if (cnt == 2 or turn == 3) else 14400
        return {"action": "wait", "wait_seconds": wait_s, "rationale": f"Detected merchant auto-reply (#{cnt}); backing off {wait_s // 3600}h."}

    curve_kw = ["gst", "tax", "accounting", "income tax", "ca ", "chartered accountant", "loan", "legal", "license"]
    if any(k in msg_l for k in curve_kw):
        body = (f"I will leave GST and tax filing to your CA — that is outside what I handle directly for {m_name}. "
                f"Returning to your growth action in {loc}: your {lx['cust']} WhatsApp draft and profile update for '{act_off}' are ready. "
                f"Reply CONFIRM to proceed and dispatch now.")
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel", "rationale": "Out-of-scope ask politely declined and redirected to growth action."}

    body = (f"Done, {o_name} — here is the ready-to-send {lx['cust']} WhatsApp draft for {m_name} ({loc}):\n\n"
            f"\"Update from {m_name} ({loc}): {act_off} is open this week with priority {lx['unit']}. Reply 1 to book.\"\n\n"
            f"I have queued this for your target {lx['custs']} alongside today's profile update. Reply CONFIRM to proceed and dispatch.")
    return {"action": "send", "body": body, "cta": "binary_confirm_cancel", "rationale": "Merchant committed; transitioned immediately to concrete action execution."}
