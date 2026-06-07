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
                productos        TEXT
            )
        """)


def save_document(data: dict):
    entidad = data.get("proveedor") or data.get("cliente") or ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO documentos (tipo, fecha_captura, fecha_documento, entidad, folio, total, productos) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data.get("tipo", ""),
                datetime.now().strftime("%d/%m/%Y %H:%M"),
                data.get("fecha", ""),
                entidad,
                data.get("folio", "") or "",
                data.get("total") or 0.0,
                json.dumps(data.get("productos", []), ensure_ascii=False),
            ),
        )


def check_duplicate(data: dict) -> str | None:
    rows = get_all_documents()
    tipo = data.get("tipo", "")
    folio = (data.get("folio") or "").strip()
    entidad = (data.get("proveedor") or data.get("cliente") or "").strip().lower()
    total = float(data.get("total") or 0)
    fecha = (data.get("fecha") or "").strip()

    for r in rows:
        if r[1] != tipo:
            continue
        r_folio = (r[5] or "").strip()
        r_entidad = (r[4] or "").strip().lower()
        r_total = float(r[6])
        r_fecha = (r[3] or "").strip()

        if folio and r_folio and folio == r_folio and entidad and entidad == r_entidad:
            return f"Ya existe un registro con folio **{folio}** de **{r[4]}**"

        if fecha and r_fecha == fecha and abs(r_total - total) < 0.01 and entidad and entidad == r_entidad:
            return f"Ya existe un registro de **{r[4]}** del **{fecha}** por **${total:,.2f}**"

    return None


def get_all_documents() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, tipo, fecha_captura, fecha_documento, entidad, folio, total, productos FROM documentos ORDER BY id DESC"
        ).fetchall()


def get_filtered_documents(start: str | None = None, end: str | None = None) -> list:
    rows = get_all_documents()
    if not start and not end:
        return rows
    result = []
    for r in rows:
        fecha_cap = r[2]  # "DD/MM/YYYY HH:MM"
        try:
            d = datetime.strptime(fecha_cap, "%d/%m/%Y %H:%M")
            if start:
                ds = datetime.strptime(start, "%d/%m/%Y")
                if d < ds:
                    continue
            if end:
                de = datetime.strptime(end, "%d/%m/%Y")
                if d > de:
                    continue
        except Exception:
            pass
        result.append(r)
    return result


def build_context(rows: list) -> str:
    if not rows:
        return "Sin registros para el período seleccionado."

    compras = [r for r in rows if r[1] == "factura_compra"]
    ventas = [r for r in rows if r[1] in ("nota_venta", "venta_publico")]
    lines = []

    if compras:
        lines.append(f"COMPRAS ({len(compras)} registros):")
        for r in compras:
            prods = json.loads(r[7])
            ps = ", ".join(
                f"{p.get('nombre','')}×{p.get('cantidad',0)}{' '+p.get('unidad','') if p.get('unidad') else ''}"
                for p in prods if p.get("nombre")
            ) or "sin detalle"
            lines.append(f"  {r[3] or '?'} | {r[4] or 'sin proveedor'} | {ps} | ${r[6]:,.2f}")

    if ventas:
        lines.append(f"\nVENTAS ({len(ventas)} registros):")
        for r in ventas:
            prods = json.loads(r[7])
            ps = ", ".join(
                f"{p.get('nombre','')}×{p.get('cantidad',0)}{' '+p.get('unidad','') if p.get('unidad') else ''}"
                for p in prods if p.get("nombre")
            ) or "sin detalle"
            entidad = r[4] or ("público general" if r[1] == "venta_publico" else "sin cliente")
            lines.append(f"  {r[3] or '?'} | {entidad} | {ps} | ${r[6]:,.2f}")

    total_c = sum(r[6] for r in compras)
    total_v = sum(r[6] for r in ventas)
    lines.append(f"\nTOTAL COMPRAS: ${total_c:,.2f} | TOTAL VENTAS: ${total_v:,.2f}")
    if total_c > 0 and total_v > 0:
        lines.append(f"MARGEN ESTIMADO: {((total_v - total_c) / total_v * 100):.1f}%")

    return "\n".join(lines)


def get_products_summary() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT productos FROM documentos WHERE tipo = 'factura_compra'").fetchall()

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
    df = pd.DataFrame(rows, columns=["ID", "Tipo", "Fecha captura", "Fecha doc", "Proveedor/Cliente", "Folio", "Total", "Productos"])
    df["Tipo"] = df["Tipo"].map(TIPO_LABEL).fillna("Desconocido")
    return df.to_csv(index=False, encoding="utf-8-sig").encode()
