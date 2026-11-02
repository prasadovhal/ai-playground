# Postflight reporting v1

The frozen analysis CSV writer omitted its `pathlib.Path` import. `run.py`
supplies that missing import without editing the frozen sources, task semantics,
scoring, allocation or endpoint gate. `audit.py` independently checks journal,
usage, identities, append-only ledger and historical protected hashes. `finalize.py`
reproduces the terminated-run report and identifies the partial observations.

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.reporting_v1.finalize
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
```

These commands are offline. Outputs are under `../analysis/`. The addendum is
specific to the eight-record terminated run and asserts its identity/counts.
Postflight tests: 2063 passed, zero failed; preflight: 2062 passed and all 2000
scripted episodes validated. The one additional regression exercises CSV output.

Live execution stopped on request 91 with provider HTTP 400 `invalid_prompt`.
Reservation `091f3198f854487a967146c812c33571` remains pending for $0.02.
Known new usage-priced spending is $0.004890695, with $0.024890695 total exposure.
No zero-dollar settlement, future encumbrance approval, prompt alteration or
paid retry occurred. Resolve the charge with externally verified billing evidence
and an append-only settlement. An audited recovery plan is also required for
the incomplete episode; do not repeat its acknowledged requests.

Historical encumbrances remain $0.068950. Available approved exposure is
$3.975109305, but it is not spendable while the pending reservation blocks execution.
Only one complete ABCD block exists, so no confirmatory p-value or interval is
reported. All original experimental files remain unchanged.
