def consume_where() -> str:
    return "status='on_shelf'"

def fridge_where() -> str:
    return "status='on_shelf' AND data_quality!='dirty' AND qty_remain>0"

def alerts_where() -> str:
    return "status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL"

def sweep_where() -> str:
    return "status='on_shelf' AND qty_remain>0"

def tag_fridge(rows: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        d["zone"] = "positive"
        out.append(d)
    return out

def consume_includes_isolated(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def alerts_treat_dirty_as_plain(lot: dict) -> bool:
    return str(lot.get("data_quality") or "") == "dirty"


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
