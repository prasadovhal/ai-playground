# Audited Luna recovery v1

The user explicitly approved retaining reservation
`091f3198f854487a967146c812c33571` as an unverified $0.02 encumbrance within the
unchanged $4 study authorization and continuing the frozen schedule. No previous
charge, result, source, report, reservation or manifest is overwritten. Approval
is recorded in approval.json; the exact additional transition is append-only,
idempotent and restricted to that reservation. Historical holds remain counted.
Another unknown charge stops execution; no future auto-approval is implemented.

Fifteen acknowledged responses/tools are restored locally. The saved invalid
agent state is preserved, not repaired. All sixteen prior attempts count toward
24 model calls; tools retain their original 24-call ceiling. The next live request
is sequence 17, constructed from the exact frozen conversation and settings.
This may be rejected again; no prompt or safeguard is altered to avoid rejection.
No acknowledged API generation is repeated. Orphan reservations or an incomplete
recovery record block automatic retries. Completed persisted episodes are audited
and skipped. Condition order and task sample stay fixed.

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v1.execute --prepare
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q 2>&1 | Out-File -Encoding utf8 competitive_prompting/phase3/luna_study_v1/recovery_v1/preflight_pytest.txt
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v1.validate
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v1.execute --approve-encumbrance
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v1.execute --execute
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v1.analysis
```

The first four commands are offline. The explicit approval transition must retain
headroom $3.975109305; it creates no confirmed charge and releases no exposure.
Every new request continues reserving/reconciling through the original controls.
The cap is a maximum, not an instruction to create extra calls after the frozen
endpoint. No recharge or cap increase is relied upon.

New outputs are in results/live and analysis here. Canonical records combine the
first seven original completed episodes, one recovered episode, and subsequent
episodes. The original failed record remains immutable audit provenance. Full
cost/tokens stay null while any usage is unknown; known tokens/cost are separate.
The frozen final-state and operational-success outcomes remain distinct.

The original fixed-500-block inference gate remains: only descriptive partial
analysis at infrastructure/budget termination. At a full endpoint, the original
family-aware analysis runs with an additional disclosed recovered-task exclusion
sensitivity. No interim p-values or outcome-driven selection. Source, approval,
checkpoint and old-ledger-prefix hashes are frozen in recovery_plan_v1.json.
