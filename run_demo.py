"""End-to-end demo: invoice -> redaction -> human gate -> rule retrieval -> advisory check.

Usage:
    python run_demo.py                 # interactive: you confirm the parsed fields
    python run_demo.py --auto-confirm  # scripted confirmation (used for the evidence log)

Writes: execution_evidence_logs.txt, dashboard.html, audit.log
"""
import argparse
import datetime
import html
import json
import os
import platform
import sys

import auraroute_core as core
import llm_adapter

LOG_LINES = []


def log(level, message):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    line = f"[{level:<5}] {ts} - {message}"
    LOG_LINES.append(line)
    print(line)


def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw and not raw.startswith("#") and "=" in raw:
                    k, v = raw.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def render_dashboard(payload, profile, result, path="dashboard.html"):
    e = html.escape
    notes = "".join(f"<li>{e(n)}</li>" for n in result["advisory_notes"])
    rules = "".join(f"<li><b>{e(r['source'])}</b> ({e(r['section'])}): {e(r['text'])}</li>"
                    for r in result["cited_rules"])
    ok = result["within_standard_quota"]
    quota_cls = "ok" if ok else ("bad" if ok is False else "")
    quota_word = "Within" if ok else ("Exceeds" if ok is False else "Check manually")
    remaining = result["remaining_quota_usd"]
    remaining_txt = f"{remaining:,.2f} USD" if remaining is not None else "no quota rule loaded"
    visa_cls = "bad" if result["visa_balance_warning"] else "ok"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AuraRoute AI - Advisory Dashboard</title>
<style>
body{{font-family:Arial,sans-serif;max-width:900px;margin:30px auto;color:#1f2937;padding:0 16px}}
h1{{color:#1e3a8a;margin-bottom:4px}} .sub{{color:#6b7280;margin-bottom:18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.card{{border:2px solid #cbd5e1;border-radius:10px;padding:12px;background:#f8fafc}}
.card.ok{{border-color:#16a34a;background:#f0fdf4}} .card.bad{{border-color:#d97706;background:#fffbeb}}
.k{{font-size:12px;color:#6b7280;text-transform:uppercase}} .v{{font-size:22px;font-weight:bold}}
.banner{{background:#fef3c7;border:1px solid #d97706;padding:8px 12px;border-radius:8px;margin:14px 0}}
</style></head><body>
<h1>AuraRoute AI: Advisory Dashboard</h1>
<div class="sub">{e(str(payload['institution']))} | invoice due {e(payload['due_date'])} | FX source: {e(result['fx_source'])}</div>
<div class="banner">Advisory planning information only. This tool does not move money or make legal determinations.</div>
<div class="grid">
<div class="card"><div class="k">Invoice</div><div class="v">{payload['amount']:,.2f} {e(payload['currency'])}</div>
<div>= {result['amount_usd']:,.2f} USD</div></div>
<div class="card {quota_cls}"><div class="k">Annual quota</div><div class="v">{quota_word}</div>
<div>Remaining: {remaining_txt}</div></div>
<div class="card {visa_cls}"><div class="k">Balance after payment</div><div class="v">{result['projected_balance_usd']:,.2f} USD</div>
<div>Illustrative buffer: {core.RULES_DATABASE['minimum_visa_maintenance_usd']:,.2f} USD</div></div>
<div class="card"><div class="k">Latest safe start date</div><div class="v">{e(result['latest_initiation_date'])}</div>
<div>Due date minus {core.RULES_DATABASE['network_clearing_buffer_days']}-day clearing buffer</div></div>
</div>
<h3>Advisory notes</h3><ul>{notes}</ul>
<h3>Cited rule snippets</h3><ul>{rules}</ul>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-confirm", action="store_true", help="simulate the user's confirmation")
    ap.add_argument("--invoice", default="data/sample_invoice.txt")
    ap.add_argument("--log-file", default="execution_evidence_logs.txt")
    args = ap.parse_args()
    load_env()
    fixer_key = os.environ.get("FIXER_API_KEY")
    plaid_token = os.environ.get("PLAID_ACCESS_TOKEN")
    mode = "LIVE" if (fixer_key or plaid_token) else "FIXTURE (offline, recorded sandbox data)"

    log("INFO", "AuraRoute AI execution evidence log")
    log("INFO", f"Run started {datetime.datetime.now(datetime.timezone.utc).isoformat()} | "
                f"Python {platform.python_version()} | {platform.system()}")
    log("INFO", f"Mode: {mode}")
    log("INFO", f"Fixer.io: {'live key set' if fixer_key else 'no key, using recorded fixture'} | "
                f"Plaid Sandbox: {'access token set' if plaid_token else 'no token, using recorded fixture'}")

    with open("data/rules_database.json", encoding="utf-8") as f:
        rules = json.load(f)
    with open("data/user_profile.json", encoding="utf-8") as f:
        profile = json.load(f)
    with open("data/regulatory_snippets.json", encoding="utf-8") as f:
        corpus = json.load(f)
    with open(args.invoice, encoding="utf-8") as f:
        raw = f.read()
    log("INFO", f"Loaded invoice '{args.invoice}', rules table, profile and {len(corpus)} rule snippets")

    schema, redacted = core.parse_and_sanitize_invoice(raw)
    log("INFO", "Layer 1: redaction applied before extraction. Text after redaction:")
    for line in redacted.splitlines():
        log("TEXT", f"  | {line}")
    log("DATA", f"Extracted: {json.dumps({k: v for k, v in schema.items() if k != 'human_verified'})}")

    llm_fn = llm_adapter.llm_from_env()
    missing = [k for k in ("amount", "currency", "due_date", "institution") if schema[k] is None]
    if missing:
        if llm_fn:
            log("INFO", f"Regex missed {missing}. Calling the model with the REDACTED text only "
                        f"(provider: {os.environ.get('LLM_PROVIDER')})")
            schema = core.llm_fallback_extract(redacted, schema, llm_fn)
            log("DATA", f"Model-filled fields after validation: {schema.get('llm_filled_fields', [])}")
            log("DATA", f"Record now: {json.dumps({k: v for k, v in schema.items() if k != 'human_verified'})}")
        else:
            log("WARN", f"Regex missed {missing} and no model is configured (set LLM_PROVIDER and a key)")

    blocked = False
    try:
        core.execute_compliance_routing_check(schema, profile, rules)
    except ValueError as exc:
        blocked = True
        log("WARN", f"Orchestrator refused the unverified payload: {exc}")
    assert blocked

    if args.auto_confirm:
        log("INFO", "Gate: user confirmation SIMULATED by --auto-confirm (fields accepted as parsed)")
        confirmation = {"amount": schema["amount"]} if schema["amount"] is not None else {}
    else:
        print(f"\nParsed: {schema['amount']} {schema['currency']} due {schema['due_date']} "
              f"({schema['institution']}), confidence {schema['parser_confidence']}")
        if input("Confirm these values? [y/N] ").strip().lower() != "y":
            log("WARN", "User did not confirm. Stopping.")
            return 1
        confirmation = {}
        for key in core.REQUIRED_FIELDS:
            if schema[key] is None:
                confirmation[key] = input(f"Enter {key}: ").strip()
        if "amount" in confirmation:
            confirmation["amount"] = float(confirmation["amount"])
        log("INFO", "Gate: user confirmed the parsed fields interactively")
    try:
        payload = core.human_in_the_loop_gate(schema, confirmation)
    except ValueError as exc:
        log("ERROR", str(exc))
        with open(args.log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG_LINES) + "\n")
        return 1
    log("INFO", f"Gate: human_verified = {payload['human_verified']}")

    query = f"{profile['home_country']} tuition annual quota splitting visa maintenance funds"
    snippets = core.retrieve_rules(query, corpus, top_k=5)
    log("INFO", "Layer 3 receives the verified payload directly; Layer 2 receives only the rule query")
    log("INFO", f"Layer 2 query: '{query}'")
    for s in snippets:
        log("DATA", f"Rule {s['id']} | {s['source_type']} | {s['source']} | section: {s['section']}")

    fx = lambda b, q: core.fetch_fx_rate(b, q, api_key=fixer_key)
    bal = lambda: core.fetch_sandbox_balances(access_token=plaid_token, fixture=profile["sandbox_accounts"])
    result = core.execute_compliance_routing_check(payload, profile, rules, fx_fn=fx, balances_fn=bal,
                                                   rule_snippets=snippets)
    log("DATA", f"FX source: {result['fx_source']} | invoice in USD: {result['amount_usd']:,.2f}")
    log("DATA", f"Total balances: {result['total_balance_usd']:,.2f} USD | "
                f"projected after payment: {result['projected_balance_usd']:,.2f} USD "
                f"(buffer {rules['minimum_visa_maintenance_usd']:,.2f})")
    log("DATA", f"Remaining annual quota: {result['remaining_quota_usd']:,.2f} USD | "
                f"within quota: {result['within_standard_quota']}")
    log("DATA", f"Latest safe initiation date: {result['latest_initiation_date']}")
    for note in result["advisory_notes"]:
        log("ADVICE", note)

    render_dashboard(payload, profile, result)
    log("INFO", "Wrote dashboard.html")
    log("INFO", f"Audit log hash chain verified: {core.verify_audit_log()}")
    log("INFO", "Run complete. No funds were moved; this tool is read-only and advisory.")

    with open(args.log_file, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
