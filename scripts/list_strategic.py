#!/usr/bin/env python3
"""Listet offene strategische Empfehlungen aus der DB."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.storage import LEARNING_DB, connect

def list_open() -> list[dict]:
    sql = """
        SELECT s.id, s.created_at, s.category, s.title, s.description,
               s.effort, s.expected_impact, s.status
          FROM strategic_recommendations s
         WHERE s.status = 'open'
         ORDER BY
           CASE s.expected_impact
             WHEN 'hoch' THEN 1 WHEN 'mittel' THEN 2 ELSE 3 END,
           CASE s.effort
             WHEN 'klein' THEN 1 WHEN 'mittel' THEN 2 ELSE 3 END
    """
    try:
        with connect(LEARNING_DB) as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]
    except Exception:
        return []

def main():
    recs = list_open()
    if not recs:
        print("Keine offenen strategischen Empfehlungen.")
        return
    print(f"{len(recs)} offene Empfehlungen:\n")
    for r in recs:
        print(f"[#{r['id']}] {r['title']} [{r['category']}]")
        print(f"  Impact: {r['expected_impact']} | Aufwand: {r['effort']}")
        print(f"  {r['description']}")
        print()

if __name__ == "__main__":
    main()
