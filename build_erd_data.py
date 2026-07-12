"""Transform scraped oracle_data.json into the graph file the ERD viewer serves.

Output (erd-data.json):
{
  "tables": { "AR_AGING_BUCKETS": {"description","primaryKey":[...],"columns":[...]} },
  "edges":  [ {"from": "CHILD", "to": "PARENT", "column": "X_ID"} ]
}

Only edges whose endpoints both exist as scraped tables are kept, so the graph
is self-consistent (no dangling nodes).
"""
import json
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "oracle_data.json"
DST = sys.argv[2] if len(sys.argv) > 2 else "erd-data.json"


def main():
    with open(SRC) as f:
        data = json.load(f)

    tables = {}
    for name, info in data.items():
        pk = info.get("primaryKey") or {}
        tables[name] = {
            "description": info.get("description", ""),
            "primaryKey": pk.get("columns", []) if isinstance(pk, dict) else [],
            "columns": [
                {"name": c["name"], "dataType": c.get("dataType", ""), "nullable": c.get("nullable", "")}
                for c in info.get("columns", [])
            ],
        }

    seen = set()
    edges = []
    for name, info in data.items():
        for fk in info.get("foreignKeys", []) or []:
            frm, to, col = fk.get("fromTable"), fk.get("toTable"), fk.get("column")
            if not (frm and to) or frm not in tables or to not in tables:
                continue
            key = (frm, to, col)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"from": frm, "to": to, "column": col})

    # Precompute degree so the viewer can size/sort nodes.
    deg = {n: 0 for n in tables}
    for e in edges:
        deg[e["from"]] += 1
        deg[e["to"]] += 1
    for n in tables:
        tables[n]["degree"] = deg[n]

    with open(DST, "w") as f:
        json.dump({"tables": tables, "edges": edges}, f)

    print(f"{len(tables)} tables, {len(edges)} edges -> {DST}")


if __name__ == "__main__":
    main()
