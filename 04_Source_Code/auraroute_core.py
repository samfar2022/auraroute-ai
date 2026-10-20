"""AuraRoute AI: core advisory logic (reference implementation).

Advisory and read-only: nothing in this module moves money or writes to an external service.
"""
import datetime
import hashlib
import json
import os
import re

# ---------------------------------------------------------------- rules data
RULES_DATABASE = {
    "network_clearing_buffer_days": 5,          # configurable constant, not a prediction
    "minimum_visa_maintenance_usd": 2500.00,   # illustrative framework baseline, NOT an official requirement; real needs vary by country and living costs
    "country_rules": {
        "CN": {
            "annual_quota_usd": 50000.00,      # SAFE individual annual FX quota
            "source": "SAFE individual FX guidance (verify current figure with your bank)",
        }
    },
}

REQUIRED_FIELDS = ("amount", "currency", "due_date")
ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "CNY", "AUD", "CAD", "JPY", "SGD", "HKD", "CHF"}
# Labeled-line redaction: each matching line is consumed whole, so multi-word values are removed.
# Covers names, passport numbers, phone numbers, email addresses, government/national ID numbers,
# plus address, student ID and date of birth. Unlabeled identifiers are NOT caught (known limitation).
PII_PATTERN = re.compile(
    r"(?im)^[ \t]*(Passport No:|Name:|Phone:|Email:|Government ID:|National ID:|ID Number:"
    r"|Address:|Student ID:|Date of Birth:)[^\n]*")
AUDIT_PATH = "audit.log"


# ------------------------------------------------- Layer 1: ingestion + redaction
def _confidence(schema):
    found = sum(schema.get(k) is not None for k in ("amount", "currency", "due_date", "institution"))
    return round(found / 4, 2)          # heuristic: share of fields matched


def parse_and_sanitize_invoice(raw_text):
    """Redact identifiers first, then extract typed fields. Missing fields stay None."""
    text = PII_PATTERN.sub(r"\1 [REDACTED]", raw_text)

    m_amount = re.search(r"Total Due:\s*[$€£]?\s*([\d,]+(?:\.\d{2})?)", text)
    m_currency = re.search(r"Currency:\s*([A-Z]{3})", text)
    m_date = re.search(r"Due Date:\s*(\d{4}-\d{2}-\d{2})", text)
    m_inst = re.search(r"Institution:\s*([^\n]+)", text)

    schema = {
        "amount": float(m_amount.group(1).replace(",", "")) if m_amount else None,
        "currency": m_currency.group(1) if m_currency else None,
        "due_date": m_date.group(1) if m_date else None,
        "institution": m_inst.group(1).strip() if m_inst else None,
        "human_verified": False,
    }
    schema["parser_confidence"] = _confidence(schema)
    return schema, text


# ------------------------------------------- LLM output validation (untrusted input)
def validate_llm_extraction(raw_output):
    """Accept model output only if it passes a strict schema and allow-list check."""
    try:
        data = json.loads(raw_output)
    except (TypeError, ValueError):
        return None
    allowed = {"amount", "currency", "due_date", "institution"}
    if not isinstance(data, dict) or set(data) - allowed:
        return None
    clean = {}
    if "amount" in data:
        a = data["amount"]
        if isinstance(a, bool) or not isinstance(a, (int, float)) or a <= 0:
            return None
        clean["amount"] = float(a)
    if "currency" in data:
        if data["currency"] not in ALLOWED_CURRENCIES:
            return None
        clean["currency"] = data["currency"]
    if "due_date" in data:
        try:
            datetime.datetime.strptime(str(data["due_date"]), "%Y-%m-%d")
        except ValueError:
            return None
        clean["due_date"] = str(data["due_date"])
    if "institution" in data:
        inst = data["institution"]
        if not isinstance(inst, str) or len(inst) > 120:
            return None
        clean["institution"] = inst.strip()
    return clean


def llm_fallback_extract(redacted_text, schema, llm_fn):
    """Fill only fields the regexes missed. llm_fn(text) returns a JSON string (untrusted)."""
    missing = [k for k in ("amount", "currency", "due_date", "institution") if schema.get(k) is None]
    if not missing:
        return schema
    clean = validate_llm_extraction(llm_fn(redacted_text))
    if clean is None:
        return schema
    out = dict(schema)
    filled = [k for k in missing if k in clean]
    for k in filled:
        out[k] = clean[k]
    out["llm_filled_fields"] = filled
    out["parser_confidence"] = _confidence(out)
    out["human_verified"] = False        # the model can never verify its own output
    return out


# ------------------------------------------------------ Human-in-the-loop gate
def human_in_the_loop_gate(schema, user_confirmation=None):
    """Nothing passes downstream until the user has confirmed every required field."""
    if user_confirmation is None:
        return schema
    verified = dict(schema)
    for key in REQUIRED_FIELDS + ("institution",):
        if key in user_confirmation:
            verified[key] = user_confirmation[key]
    missing = [k for k in REQUIRED_FIELDS if verified.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Gate refused: missing required fields {missing}")
    if float(verified["amount"]) <= 0:
        raise ValueError("Gate refused: amount must be positive")
    datetime.datetime.strptime(verified["due_date"], "%Y-%m-%d")  # raises on bad format
    verified["human_verified"] = True
    return verified


# ----------------------------------------------- Layer 2: local rule retrieval
def retrieve_rules(query, corpus, top_k=3):
    """Keyword retrieval over a static local corpus. Each hit keeps its source and section."""
    terms = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 3}
    scored = []
    for item in corpus:
        hay = (item["text"] + " " + " ".join(item.get("tags", []))).lower()
        score = sum(1 for t in terms if t in hay)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(item) for _, item in scored[:top_k]]


# ------------------------------------------------------ Read-only tool wrappers
FX_FIXTURE = {("GBP", "USD"): 1.27, ("EUR", "USD"): 1.08, ("USD", "USD"): 1.0}
FX_STATE = {"source": "fixture"}      # "live", "fixture" or "fixture-fallback"


def fetch_fx_rate(base, quote, api_key=None):
    """Read-only FX lookup. Live Fixer.io call if a key is set, recorded fixture otherwise."""
    if base == quote:
        return 1.0
    if api_key:
        try:
            import requests
            r = requests.get("https://data.fixer.io/api/latest",
                             params={"access_key": api_key, "symbols": f"{base},{quote}"},
                             timeout=10)
            r.raise_for_status()
            rates = r.json()["rates"]
            FX_STATE["source"] = "live"
            return rates[quote] / rates[base]
        except Exception as exc:       # network, auth or format failure
            audit_log("FX_LIVE_FAILED_USING_FIXTURE", {"error": type(exc).__name__})
            FX_STATE["source"] = "fixture-fallback"
            return FX_FIXTURE[(base, quote)]
    FX_STATE["source"] = "fixture"
    return FX_FIXTURE[(base, quote)]


def fetch_sandbox_balances(access_token=None, fixture=None):
    """Read-only balance lookup (Plaid Sandbox). Returns [{'amount':..., 'currency':...}]."""
    if access_token:
        import requests
        r = requests.post("https://sandbox.plaid.com/accounts/balance/get", timeout=10, json={
            "client_id": os.environ["PLAID_CLIENT_ID"],
            "secret": os.environ["PLAID_SECRET"],
            "access_token": access_token})
        r.raise_for_status()
        return [{"amount": a["balances"]["available"] or 0.0,
                 "currency": a["balances"]["iso_currency_code"] or "USD"}
                for a in r.json()["accounts"]]
    return fixture or [{"amount": 4200.00, "currency": "USD"}]   # recorded sandbox fixture


# ------------------------------------------------------------------ Audit log
def audit_log(event, details, path=None):
    """Append-only, hash-chained. Stores a hash of the details, never the details."""
    path = path or AUDIT_PATH
    prev = "0" * 64
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        if lines:
            prev = json.loads(lines[-1])["entry_sha256"]
    digest = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "event": event, "details_sha256": digest, "prev_sha256": prev}
    entry["entry_sha256"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def verify_audit_log(path=None):
    """True if every entry links to the previous one and no entry was altered."""
    path = path or AUDIT_PATH
    prev = "0" * 64
    with open(path, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            claimed = entry.pop("entry_sha256")
            recomputed = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
            if entry["prev_sha256"] != prev or recomputed != claimed:
                return False
            prev = claimed
    return True


# ------------------------------------------- Layer 3: deterministic advisory check
def execute_compliance_routing_check(payload, profile, rules=RULES_DATABASE,
                                     fx_fn=fetch_fx_rate, balances_fn=fetch_sandbox_balances,
                                     rule_snippets=None):
    if not payload.get("human_verified"):
        audit_log("BLOCKED_UNVERIFIED_PAYLOAD", {"reason": "human_verified is not True"})
        raise ValueError("Unverified payload blocked at orchestrator interface.")

    amount_usd = payload["amount"] * fx_fn(payload["currency"], "USD")
    deadline = datetime.datetime.strptime(payload["due_date"], "%Y-%m-%d")
    buffer_days = rules.get("network_clearing_buffer_days", 5)

    latest_start = deadline - datetime.timedelta(days=buffer_days)
    result = {
        "amount_usd": round(amount_usd, 2),
        "fx_source": FX_STATE["source"],
        "within_standard_quota": None,
        "remaining_quota_usd": None,
        "visa_balance_warning": False,
        "total_balance_usd": None,
        "projected_balance_usd": None,
        "latest_initiation_date": latest_start.strftime("%Y-%m-%d"),
        "advisory_notes": [],
        "cited_rules": rule_snippets or [],
    }

    country = rules["country_rules"].get(profile.get("home_country"))
    if country:
        remaining = country["annual_quota_usd"] - profile.get("annual_quota_used", 0.0)
        result["remaining_quota_usd"] = round(remaining, 2)
        result["within_standard_quota"] = amount_usd <= remaining
        if amount_usd <= remaining:
            result["advisory_notes"].append("Amount is within the remaining annual quota.")
        else:
            result["advisory_notes"].append(
                "Amount exceeds the remaining standard annual quota. Ask your bank about the "
                "documented tuition-payment route, which typically requires the admission "
                "letter and the tuition certificate. Do not split the payment.")
    else:
        result["advisory_notes"].append("No quota rule loaded for this country; check manually.")

    total_usd = sum(b["amount"] * fx_fn(b["currency"], "USD") for b in balances_fn())
    result["total_balance_usd"] = round(total_usd, 2)
    result["projected_balance_usd"] = round(total_usd - amount_usd, 2)
    if total_usd - amount_usd < rules["minimum_visa_maintenance_usd"]:
        result["visa_balance_warning"] = True
        result["advisory_notes"].append(
            "Projected post-payment balance is below the illustrative framework baseline, which is far below many real requirements. "
            "Review your maintenance-fund requirements with your institution or consulate.")

    audit_log("ROUTING_CHECK_COMPLETED", {"amount_usd": result["amount_usd"],
                                          "within_quota": result["within_standard_quota"]})
    return result
