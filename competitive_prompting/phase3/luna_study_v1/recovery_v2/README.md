# Audited continuation after HTTP 500

This continuation preserves the frozen manifest, first recovery, prior results,
and ledger prefix. User approval applies only to reservation
`625d89056f3b448eb020eefe568ed25b`: the $0.02 allowance stays unverified and is
counted exactly once. It is not a confirmed charge. Other unknown charges block.
No caps, model parameters, task allocation, condition order or scorer change.

The 13 acknowledged responses and tool calls of the interrupted episode are
replayed offline against the exact saved requests and compared to saved state.
The original 14 attempts count toward the frozen model-call limit. The new
continuation has `execution_attempt_id` linked to `planned_episode_id`, retains
the complete prior attempt and unknown usage, and starts with request sequence 15.
All earlier records and analysis are immutable. No completed episode is rerun.
At a full endpoint, the preregistered family-cluster analysis runs; the report
also includes a sensitivity excluding both infrastructure-recovered task blocks.
If stopped early, inference remains gated and only descriptive results are given.

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.execute --prepare
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q 2>&1 | Out-File -Encoding utf8 competitive_prompting/phase3/luna_study_v1/recovery_v2/preflight_pytest.txt
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.validate
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.execute --approve-encumbrance
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.execute --execute
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.analysis
```
