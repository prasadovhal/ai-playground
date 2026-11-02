# Frozen competitive prompting extension v1

This separate extension has 60 new authored cases, 30 domain families (two
cases/family), 20 cases/category, and 480 planned episodes. Original experiment
inputs, code, outputs, audits and analyses are protected by file hashes. The
spending ledger is append-only; its initial byte prefix is hash-protected.

The original prompts, model settings, tool interface, simulator, evaluator,
operational-success denominator and $6 Phase 3 / $9 experiment caps are reused.
The previously authorized $0.048950 and $0.02 encumbrances remain counted once.
No future failed request is automatically converted to an encumbrance.

Tasks combine heterogeneous independent workflows and explicit missing-prerequisite
reconciliation with already recorded downstream work, rather than renaming the
original uniform prefix-completed batches. Initial and gold counts are authored
explicitly. This is a changed difficulty distribution. The simulator cannot test
cross-record dependencies or real asynchronous/multi-operation partial writes;
before/after-commit faults represent partial workflow outcomes. See tasks.py and
manifest novelty/analysis limitations. Family labels support clustering but do
not prove independence between business domains sharing this simulator.

Ten batches each contain two tasks/category from distinct families, both models,
all conditions. Sibling examples occur in separate halves of the fixed schedule.
Seed 20261029 fixes family membership, model/task block order and condition order.
No outcome-based selection or selective reruns. Primary comparison CB, separate
model effects; whole-family cluster bootstrap and sign-flip tests. Holm adjustment
across the two primary operational tests. All other comparisons exploratory.
Original results remain separate; no automatic pooled estimate.

Freeze once, then run offline validation (never paid):

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.runner --prepare
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.runner
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q 2>&1 | Out-File -Encoding utf8 competitive_prompting/phase3/extension_v1/pytest.txt
```

Execute the frozen order, automatically progressing while admitted:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.runner --execute
```

The intended key/project are loaded from the repository-root .env without
printing credentials. Models must be available under their exact IDs; there is
no substitution. Client settings stay store=false, default tier, max_retries=0.
Before every complete ABCD block require a conservative no-cache workload
allowance plus a peak request reservation; every request then reserves/reconciles
using the original guard and approved ledger decoder. This is a conservative
admission estimate, not a guarantee. A new unknown charge immediately stops the
run. Completed persisted episodes are validated and skipped idempotently.
Reservations with missing/duplicate episode references block resumption.

The starting ledger headroom is authoritative, never the account balance.
No recharge/billing beyond credits is relied upon. Caps cannot be changed by CLI.
A budget stop records the exact shortfall to admit the next fixed block; no task
or condition is skipped. Future actual costs cannot be known exactly in advance.

Generate extension reports without API calls:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.analysis
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.reporting_v1.complete
```

The second, separately hashed reporting adapter fixes the original report's
historical contrast label (`primary_C_minus_B` versus extension `CB`), and adds
trace/budget interpretation. It does not change the frozen primary CSV,
inferential methods, experiment code or manifest. Its tests and audit are stored
separately; the first generated comparison report is preserved. Run this adapter
after the post-execution test log is complete.

Results/live contains immutable episode JSONL records, admissions and batch
validation. Analysis contains episode, condition, paired, cost, replication and
validation artifacts. Offline oracle outputs are separately labeled and never
treated as experimental model evidence. An interrupted block stays outside
paired analyses while all attempts remain in the audit.
