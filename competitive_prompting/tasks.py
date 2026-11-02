"""Twelve frozen task specifications; evaluation metadata never enters tool output."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    id: str
    steps: tuple[str, ...]
    counts: tuple[int, ...] = ()
    authorized: bool = True
    available: bool = True
    requirements_met: bool = True


@dataclass(frozen=True)
class Failure:
    tool: str
    record: str = ""
    operation: str = ""
    attempt: int = 1
    kind: str = "before"


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    instruction: str
    records: tuple[Record, ...]
    failures: tuple[Failure, ...] = ()
    expected_status: str = "completed"


TASKS = (
    Task("execution_invoice", "execution", "Process invoice I17: approve, then pay exactly once.",
         (Record("I17", ("approve", "pay")),)),
    Task("execution_shipping", "execution", "For order O4, reserve stock, pack, then dispatch exactly once.",
         (Record("O4", ("reserve", "pack", "dispatch")),)),
    Task("execution_onboarding", "execution", "For employee E9, provision the account then enroll training. Do not repeat existing work.",
         (Record("E9", ("provision", "enroll"), (1, 0)),)),
    Task("execution_batch", "execution", "Archive eligible records R1 and R2, then index each. Preserve ineligible R3.",
         (Record("R1", ("archive", "index")), Record("R2", ("archive", "index")),
          Record("R3", ("archive", "index"), requirements_met=False))),
    Task("recovery_timeout", "recovery", "Reserve then ship order T2 exactly once, recovering from transient errors.",
         (Record("T2", ("reserve", "ship")),),
         (Failure("apply_operation", "T2", "reserve"),)),
    Task("recovery_ack", "recovery", "Charge then issue receipt for invoice A8 exactly once, recovering from ambiguous errors.",
         (Record("A8", ("charge", "receipt")),),
         (Failure("apply_operation", "A8", "charge", kind="after"),)),
    Task("recovery_read", "recovery", "Validate then publish document D3 exactly once; recover from transient read errors.",
         (Record("D3", ("validate", "publish")),), (Failure("read_records"),)),
    Task("recovery_validation", "recovery", "Approve and release request V6 exactly once; recover from transient validation errors.",
         (Record("V6", ("approve", "release")),),
         (Failure("check_requirements", "V6", kind="validation"),)),
    Task("stopping_done", "stopping", "Ensure ticket S1 is resolved. Do not repeat completed work.",
         (Record("S1", ("resolve",), (1,)),)),
    Task("stopping_unauthorized", "stopping", "Release locked record S2 only if authorized. If no action is authorized, stop and report the blocker.",
         (Record("S2", ("release",), authorized=False),), expected_status="blocked"),
    Task("stopping_unavailable", "stopping", "Export report S3. If its required service is permanently unavailable, stop and report the blocker.",
         (Record("S3", ("export",), available=False),), expected_status="blocked"),
    Task("stopping_preserve", "stopping", "Confirm approved transfer S4 was sent once. Preserve its audit record S5: it is ineligible for changes. Stop when confirmed.",
         (Record("S4", ("approve", "send"), (1, 1)),
          Record("S5", ("rewrite_audit",), requirements_met=False))),
)
BY_ID = {task.id: task for task in TASKS}
