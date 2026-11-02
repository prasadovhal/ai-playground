# Luna-focused study: offline preparation only

**NOT RELEASED. No paid calls, no spending-cap changes, no frozen new task set.**

Start with [calibrated_power_report.md](calibrated_power_report.md), which uses
the observed signed-pair ICC point estimate (0.357534), then read
[readiness_report.md](readiness_report.md). The normal sample-size formulas are
starting approximations; exact family-level simulations are more conservative.
The `rho010` files are an explicitly optimistic sensitivity, not the main design.

Power is conditional on independent scenario families, their size, discordance
and correlation. The existing 54 pairs provide seven operational discordances;
they do not identify a reliable true effect or future ICC. There are no observed
B/C final-state discordances; that does not establish equivalence or zero future
variance. Operational success and state correctness remain separate outcomes.

Under q=7/54, five tasks/family and ICC about .358, 500 tasks have approximately
49% power for a five-percentage-point effect and 59% for a 5.56-point effect.
Simulation-checked sufficient sizes and their Monte Carlo intervals are in the
calibrated report. They are not exact mathematical minima or guarantees.

`candidate_protocol.json` is a **draft design contract**, not a preregistration.
It retains the four exact prompts, Luna settings and limits, primary C-B,
separate secondary outcomes, fixed-N endpoint, family-level analysis, original
denominators and no outcome-based stopping/sample-size changes. Its candidate
480 tasks are a roughly-500 budget-balanced alternative, not a validated choice.
No paid execution entry point exists in this directory.

`diversity_feasibility.json` explains the unresolved scientific gate: record-local
linear count workflows can generate many parameter variants, but not certify
96 genuinely new independent template families through renamed domains alone.
No new task IDs, oracle claims or task-set hashes have been fabricated. A richer,
separately versioned scenario design and offline validation are needed before
freezing actual tasks, allocation, sample size and endpoint implementation.
New task mechanisms may also change the cost and power assumptions.

`budget_authorization_request.json` records the exact request: Phase 3 cap
$6 -> $7.863684130 (increase $1.863684130), global cap stays $9, and new study
exposure is limited to $2.50 including active/unknown reservations. The optional
cent-rounded Phase 3 cap is $7.87; the independent local cap remains $2.50.
Neither historical unverified hold is released or classified as a confirmed
charge. The reported $4.91 balance is not verified and does not override caps.
The old 52 unfinished extension episodes remain separate and untouched.

Cost evidence uses 3,579 usage-reported Luna requests. One unknown-cost historical
episode is excluded from the complete-cost unit mean, with its reservation kept
in ledger exposure and a separate forecast stress. A 50%-contingency forecast
plus $0.02 peak allowance is approximately $2.54353 for 500 tasks or $2.44259 for
480. These are preliminary workload forecasts, not task-aware bounds or proof of
full completion within $2.50. The hard exposure guard must always take precedence.

Reproduce without API calls:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_planning_v1.prepare --simulations 2000
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_planning_v1.calibrate
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting/phase3/luna_planning_v1/tests -q
```

`preservation_audit.json` verifies the previous 539 files, including the full
ledger. `planning_snapshot_v1.json` is a hash inventory of this planning work,
explicitly not an executable study manifest. All previous results, definitions,
analysis and encumbrances remain unchanged. No commit, push or PR.
