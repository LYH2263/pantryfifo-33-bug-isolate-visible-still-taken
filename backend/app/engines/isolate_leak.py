"""Zone predicates shared by fridge / FEFO / alert bar / expire-sweep.

Isolated lots (on_shelf but dirty or non-positive remaining) must never leak
into the positive zone: not into FEFO consume candidates, not into the fridge
layers, and not into the top alert bar as plain expiry urgency. The single
source of truth is ``app.modules.quarantine.POSITIVE_WHERE``; the only path
that still sees isolated rows is the expire-sweep, which finalizes them off
the shelf (and races with clean-confirm to exactly one status).
"""

from app.modules.quarantine import POSITIVE_WHERE


def consume_where() -> str:
    """FEFO candidates: positive zone only — quarantined lots are unreachable."""
    return POSITIVE_WHERE


def fridge_where() -> str:
    """Fridge layers show the positive zone only."""
    return POSITIVE_WHERE


def alerts_where() -> str:
    """Top bar: normal expiry urgency over the positive zone only."""
    return POSITIVE_WHERE + " AND expiry IS NOT NULL"


def sweep_where() -> str:
    """Expire-sweep finalizes every on-shelf lot with remaining qty, dirty
    included — it is the removal path that also collects isolated rows."""
    return "status='on_shelf' AND qty_remain>0"


def tag_fridge(rows: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        d["zone"] = "positive"
        out.append(d)
    return out


def consume_includes_isolated(lot: dict) -> bool:
    """Whether FEFO would deduct from this lot. Mirrors ``consume_where``:
    isolated (dirty / non-positive) lots are never candidates."""
    return (
        str(lot.get("status") or "") == "on_shelf"
        and str(lot.get("data_quality") or "clean") != "dirty"
        and float(lot.get("qty_remain") or 0) > 0
    )


def alerts_treat_dirty_as_plain(lot: dict) -> bool:
    """Isolated lots never surface in the top bar as plain expiry urgency."""
    return False
