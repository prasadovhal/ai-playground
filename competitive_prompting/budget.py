"""Sequential, durable reservation ledger. Unknown charges stay reserved."""
from decimal import Decimal
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

# A deliberately narrow, one-off exception. Future unknown requests cannot use it.
ENCUMBRANCE_ID = "5996f285d52d467abb3d8e8ecd7a0433"
ENCUMBRANCE_REQUEST_ID = "req_9f5d0c83c37e448aafbb12e71f277aaf"
ENCUMBRANCE_EPISODE_ID = "phase2-pilot-20261027-v1:00:gpt-6.1-sol:execution_invoice:B"
ENCUMBRANCE_AMOUNT = Decimal("0.048950")


class BudgetError(RuntimeError):
    pass


def money(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Amounts must be finite and nonnegative")
    return number


def usage_cost(usage, pricing):
    """Reasoning tokens are already included in output_tokens; never bill twice."""
    if usage is None or any(type(usage.get(k)) is not int for k in ("input_tokens", "output_tokens")):
        return None
    input_count, output_count = usage["input_tokens"], usage["output_tokens"]
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens")
    cached = 0 if cached is None else cached
    writes = details.get("cache_write_tokens")
    unknown_writes = writes is None
    writes = 0 if unknown_writes else writes
    if any(type(n) is not int or n < 0 for n in (input_count, output_count, cached, writes)):
        return None
    if cached + writes > input_count:
        return None
    split = details.get("cache_write_tokens_details")
    writes_12h = 0
    if split is not None:
        writes_30m, writes_12h = split.get("ttl_30m_tokens"), split.get("ttl_12h_tokens")
        if (unknown_writes or any(type(n) is not int or n < 0 for n in (writes_30m, writes_12h))
                or writes_30m + writes_12h != writes):
            return None
    input_rate = (max(money(pricing["input"]), money(pricing["cache_write"]))
                  if unknown_writes else money(pricing["input"]))
    return ((input_count - cached - writes) * input_rate +
            cached * money(pricing["cached_input"]) + (writes - writes_12h) * money(pricing["cache_write"]) +
            writes_12h * money(pricing.get("cache_write_12h", money(pricing["cache_write"]) * 2)) +
            output_count * money(pricing["output"])) / Decimal(1_000_000)


def call_allowance(request, pricing):
    # Only a pre-call safety allowance, NOT measured usage or final cost.
    # Two tokens per serialized UTF-8 byte plus framing margin is intentionally
    # generous for these short text/function requests. Price input as cache writes.
    size = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
    input_bound = size * 2 + 1024
    allowance = (input_bound * max(money(pricing["input"]), money(pricing["cache_write"])) +
                 request["max_output_tokens"] * money(pricing["output"])) / Decimal(1_000_000)
    return max(Decimal("0.02"), allowance * Decimal("1.25"))


def replay_ledger(entries):
    """Validate append-only transitions and return one accounting state per ID."""
    states = {}
    for entry in entries:
        rid, event = entry["id"], entry["event"]
        if not isinstance(rid, str) or not rid or entry["stage"] not in ("pilot", "main"):
            raise BudgetError("Invalid ledger identity or stage")
        if event == "reserve":
            if rid in states:
                raise BudgetError("Duplicate ledger reservation")
            money(entry["allowance"])
            if not isinstance(entry["episode_id"], str) or not entry["episode_id"]:
                raise BudgetError("Missing reservation episode")
            states[rid] = {**entry, "state": "pending"}
            continue
        state = states.get(rid)
        if state is None or state["stage"] != entry["stage"]:
            raise BudgetError("Ledger transition has no matching reservation")
        if event == "encumbered_unverified":
            if (rid != ENCUMBRANCE_ID or state["state"] != "pending" or entry["stage"] != "pilot"
                    or state["episode_id"] != entry.get("episode_id") or entry["episode_id"] != ENCUMBRANCE_EPISODE_ID
                    or entry.get("request_id") != ENCUMBRANCE_REQUEST_ID
                    or money(state["allowance"]) != money(entry["allowance"]) or money(entry["allowance"]) != ENCUMBRANCE_AMOUNT
                    or entry.get("approval") != "explicit_user_flag" or not entry.get("approved_at_utc")):
                raise BudgetError("Invalid or repeated historical encumbrance approval")
            states[rid] = {**state, "state": "encumbered_unverified", "request_id": entry["request_id"]}
        elif event == "settle":
            if state["state"] == "settled":
                raise BudgetError("Duplicate ledger settlement")
            evidence = entry.get("external_evidence")
            if state["state"] == "encumbered_unverified" and (not isinstance(evidence, str) or not evidence.strip()):
                raise BudgetError("Externally verified evidence is required to reconcile an encumbrance")
            money(entry["cost"])
            states[rid] = {**state, "state": "settled", "cost": entry["cost"]}
        else:
            raise BudgetError("Unknown ledger event")
    return states


class Budget:
    def __init__(self, path, stage="pilot", total_cap=9, pilot_cap=0.75, remaining=9):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stage = stage
        if stage not in ("pilot", "main"):
            raise ValueError("Unknown experiment stage")
        self.total_cap = min(money(total_cap), Decimal("9.00"))
        self.pilot_cap = min(money(pilot_cap), Decimal("0.75"))
        self.entries = []
        if self.path.exists():
            self.entries = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.start_exposure = self.exposure()
        self.run_ceiling = self.start_exposure + money(remaining)
        if self.pending():
            raise BudgetError("Unsettled ledger reservation: reconcile against API billing before executing again")

    def pending(self):
        """Blocking reservations, excluding the explicitly authorized encumbrance."""
        return {rid: state for rid, state in replay_ledger(self.entries).items() if state["state"] == "pending"}

    def encumbered(self):
        return {rid: state for rid, state in replay_ledger(self.entries).items()
                if state["state"] == "encumbered_unverified"}

    def encumbered_exposure(self, stage=None):
        return sum((money(e["allowance"]) for e in self.encumbered().values()
                    if stage is None or e["stage"] == stage), Decimal(0))

    def exposure(self, stage=None):
        return sum((money(e["cost"] if e["state"] == "settled" else e["allowance"])
                    for e in replay_ledger(self.entries).values() if stage is None or e["stage"] == stage), Decimal(0))

    def reported_spend(self):
        return sum((money(e["cost"]) for e in replay_ledger(self.entries).values() if e["state"] == "settled"), Decimal(0))

    def _append(self, entry):
        replay_ledger([*self.entries, entry])
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.entries.append(entry)

    def require_available(self, allowance):
        allowance = money(allowance)
        if self.pending():
            raise BudgetError("Prior request has unknown cost; no further requests allowed")
        if self.exposure() + allowance > min(self.total_cap, self.run_ceiling):
            raise BudgetError("Experiment cap or configured remaining budget would be exceeded")
        if self.stage == "pilot" and self.exposure("pilot") + allowance > self.pilot_cap:
            raise BudgetError("Pilot cap would be exceeded")

    def reserve(self, allowance, episode_id):
        allowance = money(allowance)
        self.require_available(allowance)
        reservation = uuid.uuid4().hex
        self._append({"event": "reserve", "id": reservation, "stage": self.stage,
                      "episode_id": episode_id, "allowance": str(allowance)})
        return reservation

    def settle(self, reservation, cost, *, external_evidence=None):
        cost = money(cost)
        held = replay_ledger(self.entries)[reservation]
        entry = {"event": "settle", "id": reservation, "stage": held["stage"], "cost": str(cost)}
        if external_evidence is not None:
            if not isinstance(external_evidence, str) or not external_evidence.strip():
                raise BudgetError("External billing evidence reference must be nonempty")
            entry["external_evidence"] = external_evidence
        self._append(entry)
        if cost > money(held["allowance"]):
            raise BudgetError("Reported cost exceeded conservative allowance; stop and review pricing")


class RunLock:
    """One paid process per ledger, including across separate CLI invocations."""
    def __init__(self, ledger):
        self.path = Path(str(ledger) + ".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
        except FileExistsError:
            raise BudgetError("Paid runner already locked; inspect any stale lock before removing it") from None
        return self

    def __exit__(self, *_):
        self.path.unlink()


def approve_encumbrance(path, reservation, *, approved=False):
    """Offline, explicit transition for the single historical request; idempotent."""
    if not approved or reservation != ENCUMBRANCE_ID:
        raise BudgetError("Explicit user approval is required for the designated historical reservation only")
    path = Path(path)
    with RunLock(path):
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        state = replay_ledger(entries).get(reservation)
        if state is None or state["state"] == "settled":
            raise BudgetError("Historical reservation is missing or already settled")
        if state["state"] == "encumbered_unverified":
            return False
        entry = {"event": "encumbered_unverified", "id": reservation, "stage": state["stage"],
                 "episode_id": state["episode_id"], "request_id": ENCUMBRANCE_REQUEST_ID,
                 "allowance": str(ENCUMBRANCE_AMOUNT), "approval": "explicit_user_flag",
                 "approved_at_utc": datetime.now(timezone.utc).isoformat()}
        replay_ledger([*entries, entry])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return True
