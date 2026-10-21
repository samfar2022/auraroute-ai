# AuraRoute AI: Demo Walkthrough

This walkthrough reproduces the same demo offline, using the Python standard
library only. Evidence files are in Item 06.

## Step 1: Verify the controls (about 30 s)
python -m unittest discover -s tests -v

Expected: 29 tests pass (25 core and adapter, 4 supplementary). Covers
redaction, the human gate, hostile model output, over-quota advice, FX
fallback and audit tampering.

## Step 2: Run the pipeline (about 60 s)
python run_demo.py

Read: the redacted text (8 identifier labels replaced by [REDACTED]), then the
parsed record: 12000.0 GBP, due 2026-12-15, Example University, confidence 1.0.
Confirm the values at the human gate. Nothing proceeds without this.

## Step 3: Read the dashboard (about 60 s)
Open dashboard.html. Expected, for the synthetic sample case:
- Invoice: 12,000 GBP = 15,240 USD (fx_source = fixture)
- Quota: 8,000 USD left, so over quota. Advice: use the bank's documented
  tuition-payment route. Do not split the payment.
- Balance: 1,260 USD after paying, below the 2,500 USD illustrative baseline
- Latest safe start: 10 Dec 2026 (due 15 Dec minus a 5-day clearing buffer)

## Step 4: Safety behaviors (about 90 s)
- Unverified payload: the orchestrator raises and logs
  BLOCKED_UNVERIFIED_PAYLOAD.
- Messy invoice (no "Total Due:" label): the regex finds nothing and the
  optional model fallback may fill gaps. Note: the live model path needs your
  own provider key. Without one, tests use a mocked model reply. Say which
  you are showing.
- Audit chain: "Audit log hash chain verified: True" in
  execution_evidence_logs.txt.

## What this tool does not do
It does not hold, move, split or route money, and it makes no legal or
immigration determination. When an amount exceeds the remaining quota, it
flags it and points to the bank's documented tuition-payment channel.
The 50,000 USD annual quota (China, SAFE), the 2,500 USD baseline and the
5-day clearing buffer are configurable defaults, not verified current
requirements. Confirm them with your bank, institution or consulate.
