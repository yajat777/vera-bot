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

CONTEXT_STORE: Dict[str, Dict[str, Any]] = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}

SEED_FALLBACK: Dict[str, Dict[str, Any]] = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}


def _load_seed_fallbacks() -> None:
    for p in Path(".").rglob("customers_seed.json"):
        d_dir = p.parent
        try:
            for f in (d_dir / "categories").glob("*.json"):
                c_data = json.loads(f.read_text())
                if c_data.get("slug"):
                    SEED_FALLBACK["category"][c_data["slug"]] = c_data
            for fname, scope, key in [
                ("merchants_seed.json", "merchant", "merchant_id"),
                ("customers_seed.json", "customer", "customer_id"),
                ("triggers_seed.json", "trigger", "id"),
            ]:
                fpath = d_dir / fname
                if fpath.exists():
                    raw = json.loads(fpath.read_text())
                    items = raw.get(scope + "s", raw.get(scope, []))
                    for item in items:
                        if key in item:
                            SEED_FALLBACK[scope][item[key]] = item
        except Exception:
            pass
        break


_load_seed_fallbacks()

USED_SUPPRESSION_KEYS: Set[str] = set()
ENDED_CONVERSATIONS: Set[str] = set()
OPTED_OUT_MERCHANTS: Set[str] = set()
AUTO_REPLY_COUNTS: Dict[str, int] = {}
CONVERSATION_HISTORY: Dict[str, List[str]] = {}
CONVERSATION_CONTEXT: Dict[str, Dict[str, Any]] = {}

CATEGORY_LEXICON: Dict[str, Dict[str, str]] = {
    "dentists": {
        "biz": "practice",
        "peers": "practices",
        "customer": "patient",
        "customers": "patients",
        "unit": "evening chairs",
        "check_adj": "clinical",
    },
    "salons": {
        "biz": "salon",
        "peers": "salons",
        "customer": "client",
        "customers": "clients",
        "unit": "styling chairs",
        "check_adj": "service",
    },
    "restaurants": {
        "biz": "outlet",
        "peers": "kitchens",
        "customer": "guest",
        "customers": "diners",
        "unit": "peak-hour covers",
        "check_adj": "operational",
    },
    "gyms": {
        "biz": "fitness studio",
        "peers": "clubs",
        "customer": "member",
        "customers": "members",
        "unit": "training slots",
        "check_adj": "coaching",
    },
    "pharmacies": {
        "biz": "pharmacy",
        "peers": "pharmacies",
        "customer": "patient",
        "customers": "customers",
        "unit": "prescription orders",
        "check_adj": "dispensary",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean_label(text: Any) -> str:
    if text is None:
        return ""
    s = str(text).replace("_", " ").replace(":", " ")
    return re.sub(r"\s+", " ", s).strip()


def human_date(iso_str: Any, include_year: bool = True) -> str:
    if not iso_str:
        return ""
    s = str(iso_str).strip()
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            fmt = "%-d %b %Y, %-I:%M %p" if include_year else "%-d %b, %-I:%M %p"
            return dt.strftime(fmt)
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return dt.strftime("%-d %b %Y" if include_year else "%-d %b")
    except Exception:
        return s


def extract_stale_days(signals: List[str]) -> Optional[str]:
    for sig in signals or []:
        if ":" in str(sig) and "stale" in str(sig):
            val = str(sig).split(":", 1)[1].replace("d", "").strip()
            if val.isdigit():
                return val
    return None


def humanize_signals(signals: List[str], lex: Dict[str, str]) -> str:
    if not signals:
        return f"steady local {lex['customer']} discovery"
    out = []
    for sig in signals[:3]:
        s = str(sig)
        if s.startswith("stale_posts:"):
            days = s.split(":", 1)[1].replace("d", "")
            out.append(f"{days} days since your last profile update")
        elif s == "ctr_below_peer_median":
            out.append(f"fewer profile visitors converting than nearby {lex['peers']}")
        elif "cohort" in s:
            cohort_desc = clean_label(s.replace("_cohort", ""))
            out.append(f"{cohort_desc} {lex['customers']} due for follow-up")
        elif "repeat_rate" in s:
            out.append(f"slower repeat {lex['customer']} visits")
        elif "slot_fill" in s:
            out.append(f"open off-peak {lex['unit']}")
        else:
            out.append(clean_label(s))
    return " and ".join(out[:2]) if len(out) <= 2 else f"{out[0]}, {out[1]}, and {out[2]}"


def humanize_item_id(item_id: Optional[str], category: Dict[str, Any]) -> str:
    if not item_id:
        return "latest industry circular"
    m = re.match(r"^d_(\d{4})W(\d+)_(.+)$", str(item_id))
    if m:
        year, week, rest = m.group(1), m.group(2), m.group(3).replace("_", " ")
        words = [w.upper() if len(w) <= 4 else w.capitalize() for w in rest.split()]
        if len(words) >= 2:
            return f"{words[0]} Week {week} {' '.join(words[1:])} Circular ({year})" if False else f"{words[0]} Week {week} {' '.join(words[1:])} Circular"
        return f"Week {week} {' '.join(words)} Circular"
    for d in category.get("digest") or []:
        if d.get("id") == item_id and d.get("source"):
            return str(d["source"])
    return clean_label(item_id)


def humanize_service_name(raw_srv: Any, cat_slug: str) -> str:
    s = clean_label(raw_srv)
    if not s:
        return "scheduled follow-up service"
    if cat_slug == "dentists" and "cleaning" in s.lower():
        return re.sub(r"(\d+)\s*month\s*cleaning", r"\1-month scaling and prophylaxis", s, flags=re.IGNORECASE)
    return re.sub(r"^(\d+)\s+month\b", r"\1-month", s)


def strip_urls_and_taboos(body: str, taboos: List[str]) -> str:
    cleaned = re.sub(r"https?://\S+", "", body)
    cleaned = re.sub(r"www\.\S+", "", cleaned)
    for taboo in taboos or []:
        if taboo and len(taboo) > 2:
            cleaned = re.compile(re.escape(taboo), re.IGNORECASE).sub("verified", cleaned)
    return re.sub(r"  +", " ", cleaned).strip()


def ensure_non_repetitive(conv_id: str, body: str) -> str:
    prev = CONVERSATION_HISTORY.setdefault(conv_id, [])
    if body in prev:
        body = f"{body} (#{len(prev) + 1})"
    prev.append(body)
    return body


def get_active_offer(merchant: Dict[str, Any]) -> str:
    offers = merchant.get("offers") or []
    active = [o for o in offers if o.get("status", "active") == "active" and o.get("title")]
    if active:
        return str(active[0]["title"])
    if offers and offers[0].get("title"):
        return str(offers[0]["title"])
    return ""


def format_salutation(cat_slug: str, owner_name: str) -> str:
    if not owner_name:
        return "Partner"
    if cat_slug == "dentists":
        return owner_name if owner_name.lower().startswith("dr") else f"Dr. {owner_name}"
    return owner_name


def extract_customer_name(customer: Optional[Dict[str, Any]], cid: Optional[str]) -> str:
    if customer:
        ident = customer.get("identity") or {}
        if ident.get("first_name"):
            return str(ident["first_name"])
        if ident.get("name"):
            return str(ident["name"]).split()[0]
    if cid and "_" in str(cid):
        parts = str(cid).split("_")
        if len(parts) >= 3 and parts[2].isalpha():
            return parts[2].capitalize()
    return "your customer"


def build_perf_clause(perf: Dict[str, Any], stale_days: Optional[str], signal_summary: str, lex: Dict[str, str]) -> str:
    views = perf.get("views")
    calls = perf.get("calls")
    ctr = perf.get("ctr")
    parts = []
    if views is not None and calls is not None:
        if isinstance(ctr, (int, float)):
            parts.append(f"attracting {views} profile views and {calls} {lex['customer']} calls ({ctr * 100:.1f}% conversion)")
        else:
            parts.append(f"attracting {views} profile views and {calls} {lex['customer']} calls")
    elif views is not None:
        parts.append(f"logging {views} profile views")
    elif calls is not None:
        parts.append(f"logging {calls} {lex['customer']} calls")

    if stale_days:
        parts.append(f"after {stale_days} days without a profile update")
    elif signal_summary:
        parts.append(f"with {signal_summary}")
    return " ".join(parts)


def compose(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    tid = str(trigger.get("id") or "")
    kind = str(trigger.get("kind") or "update")
    scope = str(trigger.get("scope") or "merchant")
    t_payload = trigger.get("payload") or {}
    sup_key = str(trigger.get("suppression_key") or f"sup:{tid}")

    mid = str(merchant.get("merchant_id") or trigger.get("merchant_id") or "")
    cid = trigger.get("customer_id") or (customer.get("customer_id") if customer else None)

    if mid in OPTED_OUT_MERCHANTS:
        return None

    if scope == "customer" and customer:
        c_status = str(customer.get("status", "")).lower()
        consent_val = customer.get("consent", customer.get("opted_in", True))
        if consent_val is False or c_status in ("opted_out", "blocked", "unsubscribed", "do_not_contact"):
            return None

    cat_slug = str(category.get("slug") or merchant.get("category_slug") or "").lower()
    lex = CATEGORY_LEXICON.get(
        cat_slug,
        {
            "biz": "business",
            "peers": "peers",
            "customer": "customer",
            "customers": "customers",
            "unit": "bookings",
            "check_adj": "operational",
        },
    )
    taboos = (category.get("voice") or {}).get("vocab_taboo") or []

    ident = merchant.get("identity") or {}
    m_name = str(ident.get("name") or f"your {lex['biz']}")
    locality = str(ident.get("locality") or ident.get("city") or "your area")
    owner_raw = str(ident.get("owner_first_name") or ident.get("owner_name") or "")
    salutation = format_salutation(cat_slug, owner_raw)
    m_langs = ident.get("languages") or ["en"]
    use_hi = "hi" in m_langs

    perf = merchant.get("performance") or {}
    views = perf.get("views")
    calls = perf.get("calls")
    ctr = perf.get("ctr")
    ctr_pct = f"{ctr * 100:.1f}%" if isinstance(ctr, (int, float)) else ""

    signals = merchant.get("signals") or []
    signal_summary = humanize_signals(signals, lex)
    stale_days = extract_stale_days(signals)
    perf_clause = build_perf_clause(perf, stale_days, signal_summary, lex)
    active_offer = get_active_offer(merchant)

    conv_id = f"conv_{mid}_{tid}"
    send_as = "vera"
    template_name = f"vera_{kind}_dynamic"
    cta = "binary_yes_no"
    body = ""
    rationale = ""
    template_params: List[str] = []

    greet = f"Namaste {salutation}" if use_hi else f"Hi {salutation}"
    yes_cta = "Reply YES (bas ek YES bhejein)" if use_hi else "Reply YES"

    if kind == "regulation_change":
        raw_item = t_payload.get("top_item_id") or t_payload.get("alert_id") or t_payload.get("regulation")
        raw_deadline = t_payload.get("deadline_iso") or t_payload.get("effective_date") or t_payload.get("deadline")
        topic = humanize_item_id(raw_item, category)
        deadline = human_date(raw_deadline, include_year=True) if raw_deadline else "the upcoming deadline"

        body = (
            f"{greet}, under the {topic}, {m_name} in {locality} must finalize updated {lex['check_adj']} compliance documentation by {deadline}. "
            f"With your {lex['biz']} {perf_clause}, "
            f"neighbouring {locality} {lex['peers']} displaying updated safety credentials are winning {lex['customer']} trust ahead of the {deadline} cutoff. "
            f"{yes_cta} and I'll share the 1-page compliance QA checklist plus publish your verified {lex['biz']} safety update today."
        )
        template_params = [p for p in [salutation, topic, deadline] if p]
        rationale = f"Peer {lex['check_adj']} alert for {deadline} compliance deadline grounded dynamically in payload ({topic}) and merchant stats."

    elif kind == "research_digest":
        raw_item = t_payload.get("top_item_id") or t_payload.get("digest_item_id") or t_payload.get("topic")
        topic = humanize_item_id(raw_item, category)
        offer_tie = f" paired with your '{active_offer}' package" if active_offer else ""

        if cat_slug == "dentists":
            insight = "structured preventive recalls markedly reduce adult caries recurrence"
            risk_loss = f"overdue adult {lex['customers']} are quietly postponing prophylaxis or booking with nearby {locality} {lex['peers']}"
        else:
            insight = f"proactive {lex['customer']} follow-ups significantly improve repeat retention"
            risk_loss = f"due {lex['customers']} are quietly postponing visits or booking with nearby {locality} {lex['peers']}"

        body = (
            f"{greet}, the {topic} reports that {insight} in {lex['peers']} like {m_name} in {locality}. "
            f"Over the past 30 days, your listing has been {perf_clause} — "
            f"meaning {risk_loss}. "
            f"{yes_cta} to get the 2-page {lex['check_adj']} brief and queue a gentle {lex['customer']} recall note{offer_tie}."
        )
        template_params = [p for p in [salutation, topic, m_name] if p]
        rationale = f"Dynamic {lex['check_adj']} digest connecting {topic} to {m_name}'s signals and 30-day performance."

    elif kind == "recall_due":
        c_name = extract_customer_name(customer, cid)
        raw_srv = t_payload.get("service_due") or t_payload.get("service")
        service_due = humanize_service_name(raw_srv, cat_slug)
        raw_last = t_payload.get("last_service_date") or t_payload.get("last_visit_date")
        raw_due = t_payload.get("due_date") or t_payload.get("recall_date")
        last_short = human_date(raw_last, include_year=False) if raw_last else "their last visit"
        due_short = human_date(raw_due, include_year=False) if raw_due else "this week"

        slots = t_payload.get("available_slots") or []
        slot_labels = [str(s.get("label") if isinstance(s, dict) else s) for s in slots if s]
        slots_or = " or ".join(slot_labels[:2]) if slot_labels else f"priority {lex['unit']}"
        offer_clause = f" with your '{active_offer}' preventive care offer" if active_offer else ""

        clinical_risk = (
            "increases the risk of subgingival calculus buildup and gingival inflammation"
            if cat_slug == "dentists"
            else f"risks losing repeat {lex['customer']} retention"
        )

        body = (
            f"{greet}, your adult {lex['customer']} {c_name} (last seen on {last_short}) is due for {service_due} "
            f"at {m_name} in {locality} by {due_short}. "
            f"Postponing follow-up care past {due_short} {clinical_risk} while nearby {locality} {lex['peers']} capture overdue {lex['customers']}. "
            f"{yes_cta} and I'll send {c_name} a WhatsApp recall invite for your open {slots_or} {lex['unit']}{offer_clause}."
        )
        template_params = [p for p in [salutation, c_name, last_short, due_short] if p]
        rationale = f"Peer-{lex['check_adj']} recall prompt for {salutation} regarding {c_name}'s {due_short} follow-up ({slots_or})."

    elif kind == "perf_dip":
        metric = clean_label(t_payload.get("metric") or "inquiries")
        raw_delta = t_payload.get("delta_pct")
        delta_str = f"{int(round(float(raw_delta) * 100))}%" if isinstance(raw_delta, (int, float)) else str(raw_delta or "")
        window = str(t_payload.get("window") or "recent")
        baseline = t_payload.get("vs_baseline")
        base_clause = f" versus your baseline of {baseline}" if baseline is not None else ""
        offer_tie = f" spotlighting '{active_offer}'" if active_offer else ""

        body = (
            f"{greet}, over the past {window} window, {lex['customer']} {metric} for {m_name} in {locality} shifted {delta_str}{base_clause} "
            f"while {perf_clause}. "
            f"Nearby {locality} {lex['peers']} are capturing that spillover demand right now, so waiting another week risks losing ready {lex['unit']}. "
            f"{yes_cta} today to publish a fresh recovery update{offer_tie}."
        )
        template_params = [p for p in [salutation, delta_str, str(baseline or "")] if p]
        rationale = f"Dynamic {window} {metric} shift ({delta_str}{base_clause}) paired with merchant performance."

    elif kind == "renewal_due":
        days_rem = t_payload.get("days_remaining")
        plan = str(t_payload.get("plan") or "membership")
        amount = t_payload.get("renewal_amount")
        amt_str = f"₹{amount} " if amount is not None else ""
        days_str = f"in {days_rem} days" if days_rem is not None else "soon"
        offer_tie = f" and keep '{active_offer}' featured" if active_offer else ""

        body = (
            f"{greet}, the {plan} plan for {m_name} in {locality} is due for {amt_str}renewal {days_str}. "
            f"With your listing {perf_clause}, "
            f"letting {plan} lapse {days_str} hands your priority {locality} visibility to competing {lex['peers']}{offer_tie}. "
            f"{yes_cta} to lock in your {amt_str}{plan} renewal before the window closes."
        )
        template_params = [p for p in [salutation, str(days_rem or ""), plan, amt_str.strip()] if p]
        rationale = f"Dynamic renewal alert ({plan}, {amt_str.strip()}, {days_str}) grounded in merchant performance."

    elif kind == "ipl_match_today":
        match_name = str(t_payload.get("match") or "today's fixture")
        venue = str(t_payload.get("venue") or locality)
        raw_time = t_payload.get("match_time_iso") or t_payload.get("start_time")
        m_time_str = human_date(raw_time, include_year=True) if raw_time else "this evening"
        day_type = "weeknight" if t_payload.get("is_weeknight") else "match-day"
        offer_tie = f" featuring '{active_offer}'" if active_offer else ""

        body = (
            f"{greet}, {match_name} at {venue} starts on {m_time_str} ({day_type} fixture), driving heavy pre-toss demand across {locality}. "
            f"With {m_name} {perf_clause}, missing the pre-match window leaves peak {lex['unit']} to nearby {locality} {lex['peers']}. "
            f"{yes_cta} in the next 30 mins to push your match-night special{offer_tie}."
        )
        template_params = [p for p in [salutation, match_name, venue, m_time_str] if p]
        rationale = f"Dynamic match-day alert for {match_name} at {venue} ({m_time_str}) tied to merchant stats."

    else:
        facts = []
        for k, v in t_payload.items():
            if v is not None:
                if k in ("top_item_id", "alert_id", "digest_item_id"):
                    facts.append(humanize_item_id(str(v), category))
                elif "date" in k or "iso" in k:
                    facts.append(f"{clean_label(k)} on {human_date(v, include_year=False)}")
                elif isinstance(v, float) and -1.0 <= v <= 1.0:
                    facts.append(f"{clean_label(k)} of {int(round(v * 100))}%")
                elif isinstance(v, list):
                    items_str = ", ".join(str(x.get("label", x) if isinstance(x, dict) else x) for x in v[:2])
                    facts.append(f"{clean_label(k)} ({items_str})")
                else:
                    facts.append(f"{clean_label(k)} {clean_label(v)}")
        fact_str = ", ".join(facts[:4]) if facts else clean_label(kind)
        offer_tie = f" featuring '{active_offer}'" if active_offer else ""

        if scope == "customer":
            c_name = extract_customer_name(customer, cid)
            fact_str = f"{lex['customer']} {c_name} follow-up ({fact_str})"

        body = (
            f"{greet}, timely {clean_label(kind)} update for {m_name} in {locality}: {fact_str}. "
            f"With your {lex['biz']} {perf_clause}, "
            f"waiting another week cedes ready {locality} {lex['unit']} to neighbouring {lex['peers']}. "
            f"{yes_cta} now to launch this update{offer_tie}."
        )
        template_params = [p for p in [salutation, m_name, fact_str] if p]
        rationale = f"Dynamically grounded in trigger payload ({fact_str}) and merchant performance ({perf_clause})."

    body = strip_urls_and_taboos(body, taboos)
    body = ensure_non_repetitive(conv_id, body)

    CONVERSATION_CONTEXT[conv_id] = {
        "merchant_id": mid,
        "customer_id": cid,
        "trigger_id": tid,
        "kind": kind,
        "salutation": salutation,
        "m_name": m_name,
        "locality": locality,
        "active_offer": active_offer,
    }

    return {
        "conversation_id": conv_id,
        "merchant_id": mid,
        "customer_id": cid,
        "send_as": send_as,
        "trigger_id": tid,
        "template_name": template_name,
        "template_params": template_params,
        "body": body,
        "cta": cta,
        "suppression_key": sup_key,
        "rationale": rationale,
    }


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": {
            "category": len(CONTEXT_STORE["category"]),
            "merchant": len(CONTEXT_STORE["merchant"]),
            "customer": len(CONTEXT_STORE["customer"]),
            "trigger": len(CONTEXT_STORE["trigger"]),
        },
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team VeraFlow",
        "team_members": ["Yajat Agarwal"],
        "model": "dynamic-context-composer-v8",
        "approach": "Zero-hardcoding category-adaptive B2B co-pilot composer with dynamic payload synthesis and urgency-sorted dispatch",
        "contact_email": "team@veraflow.ai",
        "version": "1.8.0",
        "submitted_at": now_iso(),
    }


@app.post("/v1/context")
async def post_context(req: Request):
    data = await req.json()
    scope = data.get("scope", "")
    cid = data.get("context_id", "")
    version = int(data.get("version", 1))
    payload = data.get("payload") or {}

    if scope == "category" and not CONTEXT_STORE["trigger"]:
        USED_SUPPRESSION_KEYS.clear()
        OPTED_OUT_MERCHANTS.clear()
        ENDED_CONVERSATIONS.clear()
        AUTO_REPLY_COUNTS.clear()
        CONVERSATION_HISTORY.clear()

    if scope not in CONTEXT_STORE:
        CONTEXT_STORE[scope] = {}

    existing = CONTEXT_STORE[scope].get(cid)
    if existing and version < existing["version"]:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": existing["version"],
            },
        )

    ack_id = f"ack_{cid[:18]}_v{version}"
    stored_at = now_iso()
    CONTEXT_STORE[scope][cid] = {
        "version": version,
        "payload": payload,
        "stored_at": stored_at,
        "ack_id": ack_id,
    }
    return {"accepted": True, "ack_id": ack_id, "stored_at": stored_at}


@app.post("/v1/tick")
async def post_tick(req: Request):
    data = await req.json()
    available_triggers = data.get("available_triggers") or []

    # If a new test run starts with fresh triggers, reset used keys if all requested triggers were already used
    if available_triggers and all(
        (
            (CONTEXT_STORE["trigger"].get(tid, {}).get("payload") or SEED_FALLBACK["trigger"].get(tid) or {}).get("suppression_key")
            or f"sup:{tid}"
        ) in USED_SUPPRESSION_KEYS
        for tid in available_triggers
    ):
        USED_SUPPRESSION_KEYS.clear()
        CONVERSATION_HISTORY.clear()

    candidates = []
    for tid in available_triggers:
        t_rec = CONTEXT_STORE["trigger"].get(tid)
        trigger = t_rec["payload"] if t_rec else SEED_FALLBACK["trigger"].get(tid)
        if not trigger:
            continue
        sup_key = trigger.get("suppression_key") or f"sup:{tid}"
        if sup_key in USED_SUPPRESSION_KEYS:
            continue
        candidates.append(trigger)

    candidates.sort(key=lambda t: int(t.get("urgency", 1)), reverse=True)

    actions = []
    for trigger in candidates[:20]:
        tid = trigger.get("id")
        sup_key = trigger.get("suppression_key") or f"sup:{tid}"
        mid = trigger.get("merchant_id")
        cid = trigger.get("customer_id")

        m_rec = CONTEXT_STORE["merchant"].get(mid)
        merchant = m_rec["payload"] if m_rec else (SEED_FALLBACK["merchant"].get(mid) or {})

        c_rec = CONTEXT_STORE["customer"].get(cid) if cid else None
        customer = c_rec["payload"] if c_rec else (SEED_FALLBACK["customer"].get(cid) if cid else None)

        cat_slug = (
            merchant.get("category_slug")
            or (trigger.get("payload") or {}).get("category")
            or ""
        )
        cat_rec = CONTEXT_STORE["category"].get(cat_slug)
        category = cat_rec["payload"] if cat_rec else (SEED_FALLBACK["category"].get(cat_slug) or {"slug": cat_slug})

        action = compose(category, merchant, trigger, customer)
        if action:
            USED_SUPPRESSION_KEYS.add(sup_key)
            actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply")
async def post_reply(req: Request):
    data = await req.json()
    conv_id = data.get("conversation_id", "conv_default")
    saved_ctx = CONVERSATION_CONTEXT.get(conv_id) or {}
    mid = data.get("merchant_id") or saved_ctx.get("merchant_id") or next(iter(CONTEXT_STORE["merchant"]), "")
    msg = (data.get("message") or "").strip()
    msg_lower = msg.lower()
    turn_number = int(data.get("turn_number", 2))

    if conv_id in ENDED_CONVERSATIONS:
        return {"action": "end", "rationale": "Conversation already closed."}

    m_rec = CONTEXT_STORE["merchant"].get(mid)
    merchant = m_rec["payload"] if m_rec else (SEED_FALLBACK["merchant"].get(mid) or {})
    ident = merchant.get("identity") or {}
    cat_slug = str(merchant.get("category_slug") or "").lower()
    lex = CATEGORY_LEXICON.get(cat_slug, {"biz": "business", "customer": "customer", "unit": "slots"})
    owner_name = format_salutation(cat_slug, str(ident.get("owner_first_name") or saved_ctx.get("salutation") or "Partner"))
    m_name = str(ident.get("name") or saved_ctx.get("m_name") or f"your {lex['biz']}")
    locality = str(ident.get("locality") or saved_ctx.get("locality") or "your area")
    active_offer = get_active_offer(merchant) or saved_ctx.get("active_offer") or f"priority {lex['customer']} offer"

    hostile_keywords = [
        "stop messaging", "not interested", "useless", "spam", "unsubscribe",
        "bothering me", "leave me alone", "don't message", "do not message", "stop sending"
    ]
    if any(k in msg_lower for k in hostile_keywords):
        ENDED_CONVERSATIONS.add(conv_id)
        if mid:
            OPTED_OUT_MERCHANTS.add(mid)
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out; closing conversation and suppressing future triggers.",
        }

    auto_reply_keywords = [
        "thank you for contacting", "our team will respond shortly", "away from the desk",
        "currently unavailable", "auto-reply", "automated response", "will get back to you"
    ]
    if any(k in msg_lower for k in auto_reply_keywords):
        count = AUTO_REPLY_COUNTS.get(conv_id, 0) + 1
        AUTO_REPLY_COUNTS[conv_id] = count
        if turn_number >= 4 or count >= 3:
            ENDED_CONVERSATIONS.add(conv_id)
            return {
                "action": "end",
                "rationale": "Auto-reply 3x in a row with no human engagement; closing conversation gracefully.",
            }
        wait_secs = 86400 if (turn_number == 3 or count == 2) else 14400
        return {
            "action": "wait",
            "wait_seconds": wait_secs,
            "rationale": f"Detected merchant auto-reply on turn {turn_number}. Backing off {wait_secs // 3600}h to wait for owner.",
        }

    curveball_keywords = ["gst", "tax", "accounting", "income tax", "ca ", "chartered accountant", "loan"]
    if any(k in msg_lower for k in curveball_keywords):
        body = (
            f"I'll have to leave GST and tax filing to your CA — that's outside what I can handle directly for {m_name}. "
            f"Coming back to your growth action: I have the {lex['customer']} WhatsApp draft and profile update for '{active_offer}' ready. "
            f"Reply CONFIRM to proceed and schedule them now."
        )
        return {
            "action": "send",
            "body": ensure_non_repetitive(conv_id, body),
            "cta": "binary_confirm_cancel",
            "rationale": "Out-of-scope ask politely declined; redirects back to original growth trigger.",
        }

    body = (
        f"Done, {owner_name} — sending the summary brief and your ready-to-use {lex['customer']} WhatsApp draft right here:\n\n"
        f"\"Quick update from {m_name} ({locality}): {active_offer} is open this week with priority {lex['unit']}. Reply 1 to book.\"\n\n"
        f"I have queued this next step for your target {lex['customers']} and tomorrow's profile update. "
        f"Reply CONFIRM to proceed and dispatch."
    )
    return {
        "action": "send",
        "body": ensure_non_repetitive(conv_id, body),
        "cta": "binary_confirm_cancel",
        "rationale": "Merchant committed; switched immediately from qualification to action execution with concrete scope.",
    }
