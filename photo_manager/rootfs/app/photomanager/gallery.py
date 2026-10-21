"""Browsing the library: a paged list of photos with filters, for the thumbnail gallery."""
from __future__ import annotations

import time

from .db import db

FILTERS = ("all", "recent", "developed", "undeveloped")
SELECT = ("SELECT p.rel, p.taken, p.size, p.camera, p.has_jpeg, d.at, d.out_rel, u.status "
          "FROM photos p "
          "LEFT JOIN developed d ON d.rel=p.rel AND d.status='done' AND d.out_rel IS NOT NULL "
          "LEFT JOIN uploaded u ON u.out_rel=d.out_rel AND u.status='done' ")


class Gallery:
    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def _where(filter_, folder, q, days):
        where, args = [], []
        if filter_ == "recent":
            where.append("d.at IS NOT NULL AND d.at >= ?")
            args.append(time.time() - days * 86400)
        elif filter_ == "developed":
            where.append("d.out_rel IS NOT NULL")
        elif filter_ == "undeveloped":
            where.append("d.out_rel IS NULL")
        if folder:
            where.append("(p.rel LIKE ? ESCAPE '\\')")
            args.append(folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        if q:
            where.append("instr(lower(p.rel), lower(?)) > 0")
            args.append(q)
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list(self, filter_="all", folder="", q="", days=7, offset=0, limit=60) -> dict:
        filter_ = filter_ if filter_ in FILTERS else "all"
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
        days = max(1, min(int(days), 3650))
        where, args = self._where(filter_, (folder or "").strip("/"), (q or "").strip()[:100], days)
        order = "d.at DESC, p.rel" if filter_ == "recent" else "p.taken DESC, p.rel"
        with db(self.cfg) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM photos p LEFT JOIN developed d ON d.rel=p.rel "
                                 f"AND d.status='done' AND d.out_rel IS NOT NULL{where}", args).fetchone()[0]
            rows = conn.execute(f"{SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?",
                                [*args, limit, offset]).fetchall()
        items = [{"rel": rel, "taken": taken, "size": size, "camera": cam, "camera_jpeg": bool(has_jpeg),
                  "developed": out_rel is not None, "developed_at": at, "uploaded": up == "done"}
                 for rel, taken, size, cam, has_jpeg, at, out_rel, up in rows]
        return {"total": total, "offset": offset, "filter": filter_, "items": items}

    def counts(self, days=7) -> dict:
        with db(self.cfg) as conn:
            total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            dev = conn.execute("SELECT COUNT(*) FROM photos p JOIN developed d ON d.rel=p.rel "
                               "AND d.status='done' AND d.out_rel IS NOT NULL").fetchone()[0]
            recent = conn.execute("SELECT COUNT(*) FROM photos p JOIN developed d ON d.rel=p.rel "
                                  "AND d.status='done' AND d.out_rel IS NOT NULL AND d.at >= ?",
                                  (time.time() - days * 86400,)).fetchone()[0]
        return {"all": total, "developed": dev, "undeveloped": total - dev, "recent": recent, "days": days}
