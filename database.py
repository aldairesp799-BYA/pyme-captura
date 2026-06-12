from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

DB_PATH = "pyme_registros.db"

TIPO_LABEL = {
    "factura_compra": "Compra",
    "nota_venta": "Venta",
    "venta_publico": "Venta pública",
}

_COLS = ["id","tipo","fecha_captura","fecha_documento","entidad","folio","total","productos","zona","pendiente","sin_modificacion","medio"]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documentos (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo             TEXT,
                fecha_captura    TEXT,
                fecha_documento  TEXT,
                entidad          TEXT,
                folio            TEXT,
                total            REAL,
                productos        TEXT,
                zona             TEXT DEFAULT '',
                pendiente        INTEGER DEFAULT 0,
                sin_modificacion INTEGER DEFAULT 0,
                medio            TEXT DEFAULT ''
            )
        """)
        # Migration: add new columns to existing tables without failing
        for col, definition in [
            ("zona",             "TEXT DEFAULT ''"),
            ("pendiente",        "INTEGER DEFAULT 0"),
            ("sin_modificacion", "INTEGER DEFAULT 0"),
            ("medio",            "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE documentos ADD COLUMN {col} {definition}")
            except Exception:
                pass


def next_venta_publica_ref() -> str:
    """Genera referencia MM/YY-NNN para ventas al público del mes actual."""
    prefix = datetime.now().strftime("%m/%y")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT folio FROM documentos WHERE tipo='venta_publico' AND folio LIKE ?",
            (f"{prefix}-%",),
        ).fetchall()
    max_n = 0
    for (folio,) in rows:
        try:
            n = int((folio or "").split("-")[1])
            if n > max_n:
                max_n = n
        except Exception:
            pass
    return f"{prefix}-{max_n + 1:03d}"


def save_document(data: dict):
    entidad = data.get("proveedor") or data.get("cliente") or ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO documentos
               (tipo, fecha_captura, fecha_documento, entidad, folio, total, productos,
                zona, pendiente, sin_modificacion, medio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("tipo", ""),
                datetime.now().strftime("%d/%m/%Y %H:%M"),
                data.get("fecha", ""),
                entidad,
                data.get("folio", "") or "",
                data.get("total") or 0.0,
                json.dumps(data.get("productos", []), ensure_ascii=False),
                data.get("zona", "") or "",
                1 if data.get("pendiente") else 0,
                1 if data.get("sin_modificacion") else 0,
                data.get("medio", "") or "",
            ),
        )


def check_duplicate(data: dict) -> str | None:
    rows = get_all_documents()
    tipo    = data.get("tipo", "")
    folio   = (data.get("folio") or "").strip()
    entidad = (data.get("proveedor") or data.get("cliente") or "").strip().lower()
    total   = float(data.get("total") or 0)
    fecha   = (data.get("fecha") or "").strip()

    for r in rows:
        if r[1] != tipo:
            continue
        r_folio   = (r[5] or "").strip()
        r_entidad = (r[4] or "").strip().lower()
        r_total   = float(r[6])
        r_fecha   = (r[3] or "").strip()

        if folio and r_folio and folio == r_folio and entidad and entidad == r_entidad:
            return f"Ya existe un registro con folio **{folio}** de **{r[4]}**"

        if fecha and r_fecha == fecha and abs(r_total - total) < 0.01 and entidad and entidad == r_entidad:
            return f"Ya existe un registro de **{r[4]}** del **{fecha}** por **${total:,.2f}**"

    return None


def get_all_documents() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """SELECT id, tipo, fecha_captura, fecha_documento, entidad, folio, total,
                      productos, zona, pendiente, sin_modificacion, medio
               FROM documentos ORDER BY id DESC"""
        ).fetchall()


def get_filtered_documents(start: str | None = None, end: str | None = None) -> list:
    rows = get_all_documents()
    if not start and not end:
        return rows
    result = []
    for r in rows:
        fecha_cap = r[2]
        try:
            d = datetime.strptime(fecha_cap, "%d/%m/%Y %H:%M")
            if start and d < datetime.strptime(start, "%d/%m/%Y"):
                continue
            if end and d > datetime.strptime(end, "%d/%m/%Y"):
                continue
        except Exception:
            pass
        result.append(r)
    return result


def build_context(rows: list) -> str:
    if not rows:
        return "Sin registros para el período seleccionado."

    compras = [r for r in rows if r[1] == "factura_compra"]
    ventas  = [r for r in rows if r[1] in ("nota_venta", "venta_publico")]
    lines   = []

    def _ps(r):
        try:
            return ", ".join(
                f"{p['nombre']} {p.get('cantidad',0)}{p.get('unidad','')}"
                for p in json.loads(r[7] or "[]") if p.get("nombre")
            ) or "sin detalle"
        except Exception:
            return "sin detalle"

    if compras:
        lines.append(f"COMPRAS ({len(compras)} transacciones, ${sum(r[6] for r in compras):,.2f}):")
        for r in compras:
            lines.append(f"  {r[3] or '?'} | {r[4] or 'sin proveedor'} | {_ps(r)} | ${r[6]:,.2f}")

    if ventas:
        lines.append(f"\nVENTAS ({len(ventas)} transacciones, ${sum(r[6] for r in ventas):,.2f}):")
        for r in ventas:
            entidad = r[4] or ("público general" if r[1] == "venta_publico" else "sin cliente")
            zona    = f" [{r[8]}]" if r[8] else ""
            lines.append(f"  {r[3] or '?'} | {entidad}{zona} | {_ps(r)} | ${r[6]:,.2f}")

    # ── Movimiento por producto ───────────────────────────────────────────────
    pc, pv = {}, {}
    for r in compras:
        for p in json.loads(r[7] or "[]"):
            n = (p.get("nombre") or "").strip()
            if not n:
                continue
            e = pc.setdefault(n, {"cant": 0.0, "precios": []})
            e["cant"] += float(p.get("cantidad") or 0)
            if p.get("precio_unitario"):
                e["precios"].append(float(p["precio_unitario"]))
    for r in ventas:
        for p in json.loads(r[7] or "[]"):
            n = (p.get("nombre") or "").strip()
            if not n:
                continue
            e = pv.setdefault(n, {"cant": 0.0, "precios": [], "clientes": set()})
            e["cant"] += float(p.get("cantidad") or 0)
            if p.get("precio_unitario"):
                e["precios"].append(float(p["precio_unitario"]))
            if r[4]:
                e["clientes"].add(r[4])

    all_prods = sorted(set(pc) | set(pv))
    if all_prods:
        lines.append("\nMOVIMIENTO POR PRODUCTO:")
        for n in all_prods:
            c  = pc.get(n, {})
            v  = pv.get(n, {})
            cp = sum(c.get("precios", [])) / len(c["precios"]) if c.get("precios") else 0
            vp = sum(v.get("precios", [])) / len(v["precios"]) if v.get("precios") else 0
            mg = f" | margen {((vp-cp)/cp*100):+.0f}%" if cp > 0 and vp > 0 else ""
            al = " ⚠️SIN_COMPRA" if not c and v else (" (sin ventas)" if c and not v else "")
            cli = f" | {len(v.get('clientes', set()))} cliente(s)" if v.get("clientes") else ""
            lines.append(
                f"  {n}: comprado {c.get('cant',0):.1f} | vendido {v.get('cant',0):.1f}"
                f"{(' | P.c $'+f'{cp:.2f}') if cp else ''}"
                f"{(' | P.v $'+f'{vp:.2f}') if vp else ''}"
                f"{mg}{cli}{al}"
            )

    # ── Clientes ──────────────────────────────────────────────────────────────
    clis = {}
    for r in ventas:
        if r[4]:
            e = clis.setdefault(r[4], {"visitas": 0, "total": 0.0, "zona": r[8] or ""})
            e["visitas"] += 1
            e["total"]   += r[6]
    if clis:
        lines.append("\nCLIENTES:")
        for cli, d in sorted(clis.items(), key=lambda x: -x[1]["total"])[:10]:
            zona = f" [{d['zona']}]" if d["zona"] else ""
            lines.append(f"  {cli}{zona}: {d['visitas']} compra(s), ${d['total']:,.2f}")

    total_c = sum(r[6] for r in compras)
    total_v = sum(r[6] for r in ventas)
    lines.append(f"\nTOTAL COMPRAS: ${total_c:,.2f} | TOTAL VENTAS: ${total_v:,.2f}")
    if total_c > 0 and total_v > 0:
        lines.append(f"MARGEN GLOBAL: {((total_v - total_c) / total_v * 100):.1f}%")

    return "\n".join(lines)


def update_document(doc_id: int, data: dict):
    entidad = data.get("proveedor") or data.get("cliente") or ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """UPDATE documentos
               SET tipo=?, fecha_documento=?, entidad=?, folio=?, total=?,
                   productos=?, zona=?, pendiente=?
               WHERE id=?""",
            (
                data.get("tipo", ""),
                data.get("fecha", ""),
                entidad,
                data.get("folio", "") or "",
                data.get("total") or 0.0,
                json.dumps(data.get("productos", []), ensure_ascii=False),
                data.get("zona", "") or "",
                1 if data.get("pendiente") else 0,
                doc_id,
            ),
        )


def get_known_products() -> list[str]:
    """Devuelve nombres únicos de productos del historial (para normalización)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT productos FROM documentos WHERE productos != '[]'").fetchall()
    seen: dict[str, str] = {}
    for (pj,) in rows:
        try:
            for p in json.loads(pj or "[]"):
                n = (p.get("nombre") or "").strip()
                if n:
                    seen[n.lower()] = n
        except Exception:
            pass
    return list(seen.values())


def get_products_summary() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT productos FROM documentos WHERE tipo = 'factura_compra'"
        ).fetchall()

    summary = {}
    for (productos_json,) in rows:
        for p in json.loads(productos_json):
            nombre = p.get("nombre", "").strip()
            if not nombre:
                continue
            if nombre not in summary:
                summary[nombre] = {"compras": 0, "gasto_total": 0.0}
            summary[nombre]["compras"] += p.get("cantidad", 0)
            summary[nombre]["gasto_total"] += p.get("precio_total", 0.0)

    return [{"producto": k, **v} for k, v in summary.items()]


def get_all_as_df() -> "pd.DataFrame":
    rows = get_all_documents()
    cols = ["id","tipo","fecha_cap","fecha_doc","entidad","folio","total","productos",
            "zona","pendiente","sin_modificacion","medio"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


def get_capture_effectiveness() -> dict:
    """Retorna métricas de efectividad solo para registros capturados con la nueva versión (medio != '')."""
    with sqlite3.connect(DB_PATH) as conn:
        total   = conn.execute("SELECT COUNT(*) FROM documentos WHERE medio != ''").fetchone()[0]
        sin_mod = conn.execute(
            "SELECT COUNT(*) FROM documentos WHERE sin_modificacion=1 AND medio != ''"
        ).fetchone()[0]
        by_medio = conn.execute(
            """SELECT medio, COUNT(*), SUM(sin_modificacion)
               FROM documentos
               WHERE medio != ''
               GROUP BY medio"""
        ).fetchall()
    breakdown = {m: {"total": t, "sin_mod": s or 0} for m, t, s in by_medio}
    return {
        "total": total,
        "sin_modificacion": sin_mod,
        "pct": (sin_mod / total * 100) if total > 0 else 0,
        "breakdown": breakdown,
    }


def delete_all():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM documentos")
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='documentos'")
        except Exception:
            pass


def export_csv() -> bytes:
    rows = get_all_documents()
    if not rows:
        return b""
    cols = ["ID", "Tipo", "Fecha captura", "Fecha doc", "Proveedor/Cliente",
            "Folio", "Total", "Productos", "Zona", "Pendiente", "Sin modificación", "Medio"]
    df = pd.DataFrame(rows, columns=cols)
    df["Tipo"]            = df["Tipo"].map(TIPO_LABEL).fillna("Desconocido")
    df["Pendiente"]       = df["Pendiente"].map({0: "No", 1: "Sí"})
    df["Sin modificación"] = df["Sin modificación"].map({0: "No", 1: "Sí"})
    return df.to_csv(index=False, encoding="utf-8-sig").encode()
