"""Read queries. Every function takes a cursor opened by ``db.transaction(tenant)``, so row-level
security decides visibility; nothing here filters by tenant in SQL text.

Pagination (IDTA-01002 "Pagination"): deterministic order by an internal sequence; the cursor is an
opaque base64url token naming the last position served; ``paging_metadata.cursor`` is omitted on
the last page."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

import psycopg

from .errors import bad_request

GLOBAL_ASSET_ID = "globalAssetId"


@dataclass(frozen=True)
class Page:
    items: list
    next_cursor: str | None

    def body(self) -> dict:
        meta = {"cursor": self.next_cursor} if self.next_cursor else {}
        return {"paging_metadata": meta, "result": self.items}


def encode_cursor(position: int) -> str:
    return base64.urlsafe_b64encode(f"p:{position}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if cursor == "":
        raise bad_request("bad_cursor", "cursor must not be empty")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, _, number = raw.partition(":")
        position = int(number)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise bad_request("bad_cursor", "cursor is not one this server issued") from None
    if kind != "p" or position < 0:
        raise bad_request("bad_cursor", "cursor is not one this server issued")
    return position


def page_list(items: list, cursor: str | None, limit: int) -> Page:
    """Pagination over an in-document list (submodel refs, submodel elements): position = index."""
    start = decode_cursor(cursor)
    chunk = items[start : start + limit]
    more = start + limit < len(items)
    return Page(chunk, encode_cursor(start + limit) if more else None)


def canonical_reference(ref: dict) -> str:
    keys = [{"type": k.get("type"), "value": k.get("value")} for k in ref.get("keys", [])]
    return json.dumps({"keys": keys, "type": ref.get("type")}, sort_keys=True, separators=(",", ":"))


def normalise_asset_name(name: str) -> str:
    return GLOBAL_ASSET_ID if name.lower() == GLOBAL_ASSET_ID.lower() else name


# ---------------------------------------------------------------------------------------------
# shells
# ---------------------------------------------------------------------------------------------


def _asset_filter(pairs: list[tuple[str, str]]) -> tuple[str, list]:
    clauses, params = [], []
    for name, value in pairs:
        clauses.append("EXISTS (SELECT 1 FROM asset_ids a WHERE a.shell_id = s.id AND a.name = %s AND a.value = %s)")
        params += [normalise_asset_name(name), value]
    return " AND ".join(clauses), params


def list_shells(
    cur: psycopg.Cursor, pairs: list[tuple[str, str]], id_short: str | None, cursor: str | None, limit: int
) -> Page:
    after = decode_cursor(cursor)
    where, params = ["s.seq > %s"], [after]
    if id_short is not None:
        where.append("s.id_short = %s")
        params.append(id_short)
    if pairs:
        clause, extra = _asset_filter(pairs)
        where.append(clause)
        params += extra
    cur.execute(
        f"SELECT s.seq, s.doc FROM shells s WHERE {' AND '.join(where)} ORDER BY s.seq LIMIT %s",  # noqa: S608
        [*params, limit + 1],
    )
    rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return Page([r["doc"] for r in rows], encode_cursor(rows[-1]["seq"]) if more else None)


def list_shell_ids_by_assets(cur: psycopg.Cursor, pairs: list[tuple[str, str]], cursor: str | None, limit: int) -> Page:
    after = decode_cursor(cursor)
    where, params = ["s.seq > %s"], [after]
    if pairs:
        clause, extra = _asset_filter(pairs)
        where.append(clause)
        params += extra
    cur.execute(
        f"SELECT s.seq, s.id FROM shells s WHERE {' AND '.join(where)} ORDER BY s.seq LIMIT %s",  # noqa: S608
        [*params, limit + 1],
    )
    rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return Page([r["id"] for r in rows], encode_cursor(rows[-1]["seq"]) if more else None)


def get_shell(cur: psycopg.Cursor, shell_id: str) -> dict | None:
    cur.execute("SELECT doc FROM shells WHERE id = %s", (shell_id,))
    row = cur.fetchone()
    return row["doc"] if row else None


def shell_row(cur: psycopg.Cursor, shell_id: str) -> dict | None:
    cur.execute("SELECT id, kind, tenant_id, doc FROM shells WHERE id = %s", (shell_id,))
    return cur.fetchone()


def asset_links(cur: psycopg.Cursor, shell_id: str) -> list[dict] | None:
    if get_shell(cur, shell_id) is None:
        return None
    cur.execute("SELECT doc FROM asset_ids WHERE shell_id = %s ORDER BY position", (shell_id,))
    return [r["doc"] for r in cur.fetchall()]


# ---------------------------------------------------------------------------------------------
# submodels
# ---------------------------------------------------------------------------------------------


def list_submodels(
    cur: psycopg.Cursor,
    semantic_ref: str | None,
    semantic_value: str | None,
    id_short: str | None,
    cursor: str | None,
    limit: int,
) -> Page:
    after = decode_cursor(cursor)
    where, params = ["seq > %s"], [after]
    if id_short is not None:
        where.append("id_short = %s")
        params.append(id_short)
    if semantic_ref is not None:
        where.append("%s = ANY (semantic_refs)")
        params.append(semantic_ref)
    if semantic_value is not None:
        where.append("%s = ANY (semantic_values)")
        params.append(semantic_value)
    cur.execute(
        f"SELECT seq, doc FROM submodels WHERE {' AND '.join(where)} ORDER BY seq LIMIT %s",  # noqa: S608
        [*params, limit + 1],
    )
    rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return Page([r["doc"] for r in rows], encode_cursor(rows[-1]["seq"]) if more else None)


def get_submodel(cur: psycopg.Cursor, submodel_id: str) -> dict | None:
    cur.execute("SELECT doc FROM submodels WHERE id = %s", (submodel_id,))
    row = cur.fetchone()
    return row["doc"] if row else None


def shell_references_submodel(shell: dict, submodel_id: str) -> bool:
    for ref in shell.get("submodels", []):
        keys = ref.get("keys", [])
        if keys and keys[-1].get("type") == "Submodel" and keys[-1].get("value") == submodel_id:
            return True
    return False


# ---------------------------------------------------------------------------------------------
# passport events (MADFAM API)
# ---------------------------------------------------------------------------------------------


def list_passport_events(cur: psycopg.Cursor, shell_id: str, cursor: str | None, limit: int) -> Page:
    after = decode_cursor(cursor)
    cur.execute(
        """
        SELECT seq, event_id, submodel_id, position, doc, content_sha256, received_at
        FROM passport_events WHERE shell_id = %s AND seq > %s ORDER BY seq LIMIT %s
        """,
        (shell_id, after, limit + 1),
    )
    rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "eventId": str(r["event_id"]),
            "submodelId": r["submodel_id"],
            "position": r["position"],
            "contentSha256": r["content_sha256"],
            "receivedAt": r["received_at"].isoformat(),
            "event": r["doc"],
        }
        for r in rows
    ]
    return Page(items, encode_cursor(rows[-1]["seq"]) if more else None)
