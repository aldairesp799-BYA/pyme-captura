import io
import json
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Streamlit Cloud secrets bridge (antes de importar extractor)
try:
    for _k in ["GROQ_API_KEY"]:
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

from database import (
    TIPO_LABEL, build_context, check_duplicate, delete_all,
    export_csv, get_all_as_df, get_all_documents,
    get_capture_effectiveness, get_filtered_documents,
    get_known_products, init_db, next_venta_publica_ref,
    save_document, update_document,
    get_catalogo_clientes, upsert_cliente, delete_cliente, find_cliente,
    get_catalogo_proveedores, upsert_proveedor, delete_proveedor, find_proveedor,
    get_catalogo_productos, upsert_producto, delete_producto, find_producto_canonico,
    learn_alias,
)
from extractor import (
    analyze_business, chat_with_agent,
    extract_catalogo_entry,
    extract_from_audio_auto, extract_from_excel,
    extract_from_image_auto, extract_from_text_auto,
    normalize_product_names,
)

st.set_page_config(page_title="Verstockia", page_icon="📦", layout="centered")
init_db()

st.markdown("""
<style>
[data-testid="stMetric"] {
    background:#f8fafc; border-radius:12px; padding:14px;
    border:1px solid #e2e8f0; text-align:center;
}
.stTabs [data-baseweb="tab"] { font-size:15px; font-weight:600; }
div[data-testid="stVerticalBlock"] > div:has(> [data-testid="stButton"]) button[kind="primary"] {
    height:64px; font-size:18px;
}
[data-testid="stCameraInput"] > div { border-radius:16px; overflow:hidden; }
[data-testid="stCameraInput"] video { min-height:260px; object-fit:cover; }
[data-testid="stCameraInput"] button {
    height:56px !important; font-size:17px !important;
    border-radius:12px !important;
}
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ─────────────────────────────────────────────────────
for _k, _v in [("upload_key", 0), ("camera_open", False)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Helpers ───────────────────────────────────────────────────────────────────

TIPO_OPTIONS = {
    "🛒 Factura de compra":           "factura_compra",
    "📝 Nota de venta":               "nota_venta",
    "🏪 Venta al público (sin nota)": "venta_publico",
}
TIPO_COLOR = {"factura_compra": "#ef4444", "nota_venta": "#22c55e", "venta_publico": "#22c55e"}
PROD_COLS = {
    "nombre":          st.column_config.TextColumn("Producto", required=True),
    "cantidad":        st.column_config.NumberColumn("Cant.", format="%.2f"),
    "unidad":          st.column_config.TextColumn("Unidad"),
    "precio_unitario": st.column_config.NumberColumn("P. unit.", format="$%.2f"),
    "precio_total":    st.column_config.NumberColumn("Total", format="$%.2f"),
}
EMPTY_PROD = {"nombre":"","cantidad":0,"unidad":"","precio_unitario":0.0,"precio_total":0.0}

MEDIO_LABEL = {
    "camara":           "📷 Cámara",
    "galeria":          "🖼 Galería",
    "audio_grabacion":  "🎙 Audio (grabación)",
    "audio_archivo":    "📁 Audio (archivo)",
    "texto":            "✏️ Texto",
    "excel":            "📊 Excel/CSV",
    "whatsapp_imagen":  "📱 WhatsApp foto",
    "whatsapp_audio":   "📱 WhatsApp audio",
    "whatsapp_texto":   "📱 WhatsApp texto",
}


def _norm_prods(prods):
    out = [{"nombre":p.get("nombre",""),"cantidad":p.get("cantidad",0),
            "unidad":p.get("unidad",""),"precio_unitario":p.get("precio_unitario",0.0),
            "precio_total":p.get("precio_total",0.0)} for p in (prods or [])]
    return out or [EMPTY_PROD.copy()]


def _parse_df_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fecha"] = pd.to_datetime(df["fecha_doc"], format="%d/%m/%Y", errors="coerce")
    mask = df["fecha"].isna()
    df.loc[mask, "fecha"] = pd.to_datetime(
        df.loc[mask, "fecha_cap"].str[:10], format="%d/%m/%Y", errors="coerce"
    )
    return df


def _groq(fn, *args, **kwargs):
    """Llama a una función del extractor y muestra mensaje amigable si Groq está limitado."""
    try:
        return fn(*args, **kwargs)
    except RuntimeError as e:
        st.error(f"⏳ {e}")
        st.stop()


def _normalize(data: dict) -> dict:
    # Entidad: buscar nombre canónico en catálogo
    for field, finder in (("proveedor", find_proveedor), ("cliente", find_cliente)):
        val = (data.get(field) or "").strip()
        if val:
            match = finder(val)
            if match:
                data[field] = match["nombre"]

    # Productos: primero catálogo (exacto/alias), luego LLM contra historial
    prods = data.get("productos") or []
    for p in prods:
        nombre = (p.get("nombre") or "").strip()
        if nombre:
            canonical = find_producto_canonico(nombre)
            if canonical:
                p["nombre"] = canonical
    if prods:
        known = get_known_products()
        if len(known) >= 3:
            data["productos"] = normalize_product_names(prods, known)
    return data


def _process_and_draft(raw_bytes: bytes, filename: str = "imagen.jpg", medio: str = "galeria"):
    data = _normalize(_groq(extract_from_image_auto, raw_bytes))
    st.session_state["cap_draft"] = data
    st.session_state["cap_medio"] = medio
    st.session_state.pop("cap_mode", None)


def _clear_draft():
    for k in ["cap_draft", "_bytes", "_name", "cap_transcript", "cap_medio",
              "agente_analisis", "agente_period",
              "qr_prods_edit", "qr_prods_resolved", "qr_prods_sig"]:
        st.session_state.pop(k, None)
    st.session_state["upload_key"] += 1
    st.session_state["camera_open"] = False


# ── TABS ──────────────────────────────────────────────────────────────────────
st.title("📦 Verstockia")
tab_cat, tab_cap, tab_home, tab_reg, tab_agent = st.tabs(["📚 Catálogo", "📸 Capturar", "📊 Inicio", "📋 Registros", "🤖 Agente IA"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — INICIO / DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab_home:
    df_all = _parse_df_dates(get_all_as_df())

    if df_all.empty:
        st.info("Aún no hay registros. Ve a la pestaña **📸 Capturar** para empezar.")
    else:
        today       = pd.Timestamp(date.today())
        week_start  = today - pd.Timedelta(days=today.dayofweek)
        month_start = today.replace(day=1)
        year_start  = today.replace(month=1, day=1)
        prev_week_start = week_start - pd.Timedelta(weeks=1)

        # ── Selector de período ───────────────────────────────────────────────
        dash_p = st.selectbox(
            "Período", ["Hoy", "Esta semana", "Este mes", "Este año", "Todo"],
            index=2, key="dash_period",
        )
        _mask = {
            "Hoy":         df_all["fecha"].dt.normalize() == today.normalize(),
            "Esta semana": df_all["fecha"] >= week_start,
            "Este mes":    df_all["fecha"] >= month_start,
            "Este año":    df_all["fecha"] >= year_start,
            "Todo":        pd.Series(True, index=df_all.index),
        }
        df_f    = df_all[_mask[dash_p]]
        compras = df_f[df_f["tipo"] == "factura_compra"]
        ventas  = df_f[df_f["tipo"].isin(["nota_venta","venta_publico"])]

        # ── Métricas del período ──────────────────────────────────────────────
        c_tot = compras["total"].sum()
        v_tot = ventas["total"].sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📥 Compras",        f"${c_tot:,.0f}")
        c2.metric("📤 Ventas",         f"${v_tot:,.0f}")
        c3.metric("Registros compra",  len(compras))
        c4.metric("Registros venta",   len(ventas))

        # ── KPI efectividad de captura (solo registros nuevos con medio rastreado) ──
        kpi = get_capture_effectiveness()
        if kpi["total"] > 0:
            st.markdown("---")
            st.markdown("#### 🎯 Efectividad de captura")
            k1, k2, k3 = st.columns(3)
            k1.metric("Registros rastreados",  kpi["total"])
            k2.metric("Sin corrección manual", kpi["sin_modificacion"])
            k3.metric("Efectividad",           f"{kpi['pct']:.0f}%")
            if kpi["breakdown"]:
                bd_data = [
                    {
                        "Medio": MEDIO_LABEL.get(m, m),
                        "Total": v["total"],
                        "Sin corrección": v["sin_mod"],
                        "%": f"{v['sin_mod']/v['total']*100:.0f}%" if v["total"] else "—",
                    }
                    for m, v in kpi["breakdown"].items()
                ]
                with st.expander("Desglose por medio de captura"):
                    st.dataframe(pd.DataFrame(bd_data), hide_index=True, use_container_width=True)

        # ── Alertas semana vs semana anterior (siempre sobre datos globales) ──
        compras_all = df_all[df_all["tipo"] == "factura_compra"]
        ventas_all  = df_all[df_all["tipo"].isin(["nota_venta","venta_publico"])]
        c_sem  = compras_all[compras_all["fecha"] >= week_start]["total"].sum()
        v_sem  = ventas_all[ventas_all["fecha"] >= week_start]["total"].sum()
        c_prev = compras_all[(compras_all["fecha"] >= prev_week_start) & (compras_all["fecha"] < week_start)]["total"].sum()
        v_prev = ventas_all[(ventas_all["fecha"] >= prev_week_start) & (ventas_all["fecha"] < week_start)]["total"].sum()
        alerts = []
        if c_prev > 0 and c_sem > 0:
            pct_c = (c_sem - c_prev) / c_prev * 100
            if pct_c > 20:
                alerts.append(f"📈 Compras esta semana **{pct_c:.0f}% más** que la semana pasada (${c_sem:,.0f} vs ${c_prev:,.0f})")
            elif pct_c < -20:
                alerts.append(f"📉 Compras esta semana **{abs(pct_c):.0f}% menos** que la semana pasada — bien controlado")
        if v_prev > 0 and v_sem > 0:
            pct_v = (v_sem - v_prev) / v_prev * 100
            if pct_v > 20:
                alerts.append(f"🚀 Ventas esta semana **{pct_v:.0f}% más** que la semana pasada (${v_sem:,.0f} vs ${v_prev:,.0f})")
            elif pct_v < -20:
                alerts.append(f"⚠️ Ventas esta semana **{abs(pct_v):.0f}% menores** que la semana pasada")
        for a in alerts:
            st.info(a)

        # ── Gráfica de actividad (del período seleccionado) ───────────────────
        st.subheader("Actividad")
        df_w = df_f[df_f["fecha"].notna()].copy()
        df_w["semana"] = df_w["fecha"].dt.to_period("W").dt.start_time
        df_w["Tipo"]   = df_w["tipo"].map({"factura_compra":"Compras","nota_venta":"Ventas","venta_publico":"Ventas"})
        weekly = df_w.groupby(["semana","Tipo"])["total"].sum().reset_index()
        if not weekly.empty:
            fig = px.bar(
                weekly, x="semana", y="total", color="Tipo",
                color_discrete_map={"Compras":"#ef4444","Ventas":"#22c55e"},
                labels={"semana":"Semana","total":"Total ($)"},
                barmode="group",
            )
            fig.update_layout(margin=dict(t=20,b=20), legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("No hay datos con fecha para mostrar la gráfica.")

        # ── Top proveedores | Top clientes ────────────────────────────────────
        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Top proveedores")
            if not compras.empty:
                top_prov = (
                    compras[compras["entidad"] != ""]
                    .groupby("entidad")["total"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={"entidad":"Proveedor","total":"Total ($)"})
                )
                if not top_prov.empty:
                    fig2 = px.bar(top_prov.sort_values("Total ($)"), x="Total ($)", y="Proveedor",
                                  orientation="h", color_discrete_sequence=["#ef4444"])
                    fig2.update_layout(margin=dict(t=10,b=10), showlegend=False)
                    st.plotly_chart(fig2, use_container_width=True)
                else:
                    st.caption("Sin datos de proveedores.")
            else:
                st.caption("Sin compras en este período.")
        with col_b:
            st.subheader("Top clientes")
            ventas_id = ventas[ventas["entidad"].notna() & (ventas["entidad"] != "")]
            if not ventas_id.empty:
                top_cli = (
                    ventas_id.groupby("entidad")["total"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={"entidad":"Cliente","total":"Total ($)"})
                )
                if not top_cli.empty:
                    fig_cli = px.bar(top_cli.sort_values("Total ($)"), x="Total ($)", y="Cliente",
                                     orientation="h", color_discrete_sequence=["#22c55e"])
                    fig_cli.update_layout(margin=dict(t=10,b=10), showlegend=False)
                    st.plotly_chart(fig_cli, use_container_width=True)
                else:
                    st.caption("Sin clientes identificados.")
            else:
                st.caption("Sin ventas con cliente en este período.")

        # ── Top productos: toggle monto / volumen ─────────────────────────────
        st.markdown("---")
        prod_metric = st.radio("📊 Ver productos por", ["💰 Monto ($)", "📦 Volumen (unidades)"],
                               horizontal=True, key="prod_metric")

        def _top_prods_fig(df_rows, color):
            items = []
            for _, row in df_rows.iterrows():
                try:
                    for p in json.loads(row["productos"]):
                        if p.get("nombre"):
                            items.append({
                                "Producto": p["nombre"],
                                "Monto":    float(p.get("precio_total", 0) or 0),
                                "Volumen":  float(p.get("cantidad", 0) or 0),
                            })
                except Exception:
                    pass
            if not items:
                return None
            df_i = pd.DataFrame(items).groupby("Producto")[["Monto","Volumen"]].sum().reset_index()
            if prod_metric == "💰 Monto ($)":
                df_i = df_i.nlargest(5, "Monto").sort_values("Monto")
                return px.bar(df_i, x="Monto", y="Producto", orientation="h",
                              color_discrete_sequence=[color], labels={"Monto":"Total ($)"})
            else:
                df_i = df_i.nlargest(5, "Volumen").sort_values("Volumen")
                return px.bar(df_i, x="Volumen", y="Producto", orientation="h",
                              color_discrete_sequence=[color], labels={"Volumen":"Cantidad"})

        col_c, col_d = st.columns(2)
        with col_c:
            st.subheader("Productos comprados")
            fig_pc = _top_prods_fig(compras, "#f97316")
            if fig_pc:
                fig_pc.update_layout(margin=dict(t=10,b=10), showlegend=False)
                st.plotly_chart(fig_pc, use_container_width=True)
            else:
                st.caption("Sin detalle de productos.")
        with col_d:
            st.subheader("Productos vendidos")
            fig_pv = _top_prods_fig(ventas, "#3b82f6")
            if fig_pv:
                fig_pv.update_layout(margin=dict(t=10,b=10), showlegend=False)
                st.plotly_chart(fig_pv, use_container_width=True)
            else:
                st.caption("Sin detalle de productos vendidos.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — CAPTURAR
# ══════════════════════════════════════════════════════════════════════════════
with tab_cap:

    # ── REVISIÓN RÁPIDA ───────────────────────────────────────────────────────
    if "cap_draft" in st.session_state:
        draft   = st.session_state["cap_draft"]
        tipo    = draft.get("tipo", "factura_compra")
        total   = draft.get("total") or 0
        fecha   = draft.get("fecha") or ""
        entidad = draft.get("proveedor") or draft.get("cliente") or ""
        folio   = draft.get("folio") or ""
        prods   = draft.get("productos") or []

        tipo_label = {
            "factura_compra": "🛒 Compra",
            "nota_venta":     "📝 Venta",
            "venta_publico":  "🏪 Venta pública",
        }.get(tipo, "📄 Documento")

        st.subheader(f"{tipo_label} · ${float(total):,.2f}")

        # Transcripción (si viene de audio)
        if st.session_state.get("cap_transcript"):
            with st.expander("📝 Transcripción del audio"):
                st.write(st.session_state["cap_transcript"])

        # ── Alertas de datos faltantes ────────────────────────────────────────
        if not fecha:
            st.warning("⚠️ No se detectó fecha — ingresa cuándo fue la operación.")
        if not entidad and tipo == "factura_compra":
            st.warning("⚠️ No se detectó proveedor.")
        if not folio and tipo == "nota_venta":
            st.info("ℹ️ Sin folio de nota — puedes marcarlo como pendiente abajo.")

        # ── Campos editables ──────────────────────────────────────────────────
        col1, col2 = st.columns(2)
        with col1:
            new_tipo_lbl = st.selectbox(
                "Tipo", list(TIPO_OPTIONS.keys()),
                index=list(TIPO_OPTIONS.values()).index(tipo) if tipo in TIPO_OPTIONS.values() else 0,
                key="qr_tipo",
            )
            new_tipo = TIPO_OPTIONS[new_tipo_lbl]
        with col2:
            new_fecha = st.text_input(
                "Fecha del documento *",
                value=fecha,
                placeholder=date.today().strftime("%d/%m/%Y"),
                key="qr_fecha",
            )

        ent_label = "Proveedor" if new_tipo == "factura_compra" else "Cliente"
        col3, col4 = st.columns(2)
        with col3:
            new_entidad = st.text_input(ent_label, value=entidad, key="qr_entidad")
        with col4:
            new_total = st.number_input("Total ($)", value=float(total), min_value=0.0, format="%.2f", key="qr_total")

        # Folio / Referencia
        col5, col6 = st.columns(2)
        with col5:
            if new_tipo == "venta_publico":
                default_ref = folio if folio else next_venta_publica_ref()
                new_folio = st.text_input("Referencia (auto)", value=default_ref, key="qr_folio",
                                          help="Generada automáticamente: MMAA-NNN")
            else:
                new_folio = st.text_input("Folio", value=folio, key="qr_folio",
                                          placeholder="Número de folio (opcional)")

        # Zona (solo para ventas) — auto-rellena desde catálogo si el cliente ya existe
        with col6:
            if new_tipo in ("nota_venta", "venta_publico"):
                _zona_draft = draft.get("zona", "") or ""
                if not _zona_draft and new_entidad.strip():
                    _cat_cli = find_cliente(new_entidad.strip())
                    if _cat_cli and _cat_cli.get("zona"):
                        _zona_draft = _cat_cli["zona"]
                new_zona = st.text_input("Zona / colonia", value=_zona_draft,
                                         key="qr_zona", placeholder="Ej: Centro, Col. Juárez")
            else:
                new_zona = ""

        # Pendiente (nota_venta sin folio)
        new_pendiente = False
        if new_tipo == "nota_venta" and not new_folio.strip():
            new_pendiente = st.checkbox("⏳ Marcar como pendiente por confirmar (sin folio)", value=True, key="qr_pendiente")

        # ── Productos ─────────────────────────────────────────────────────────
        if prods:
            with st.expander(f"📦 {len(prods)} producto(s) detectados", expanded=True):
                st.dataframe(
                    pd.DataFrame(_norm_prods(prods))[["nombre","cantidad","unidad","precio_total"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "nombre":       st.column_config.TextColumn("Producto"),
                        "cantidad":     st.column_config.NumberColumn("Cant.", format="%.0f"),
                        "unidad":       st.column_config.TextColumn("Unidad"),
                        "precio_total": st.column_config.NumberColumn("Total", format="$%.2f"),
                    }
                )

        # ── Editor de productos con precio_total auto-calculado ──────────────────
        _edit_key = "qr_prods_edit"
        _base_key = "qr_prods_resolved"
        _sig_key  = "qr_prods_sig"

        # Reset cuando llega un nuevo draft
        _sig = json.dumps(prods, sort_keys=True, ensure_ascii=False)
        if st.session_state.get(_sig_key) != _sig:
            st.session_state[_base_key] = _norm_prods(prods)
            st.session_state[_sig_key]  = _sig
            st.session_state.pop(_edit_key, None)

        # Leer delta acumulado del data_editor
        _delta = st.session_state.get(_edit_key, {})
        _base  = [dict(r) for r in st.session_state.get(_base_key, _norm_prods(prods))]

        if _delta:
            for _si, _ch in (_delta.get("edited_rows") or {}).items():
                _i = int(_si)
                if _i < len(_base):
                    _base[_i].update(_ch)
            for _di in sorted((_delta.get("deleted_rows") or []), reverse=True):
                if _di < len(_base):
                    _base.pop(_di)
            for _nr in (_delta.get("added_rows") or []):
                _base.append({"nombre":"","cantidad":0,"unidad":"","precio_unitario":0.0,"precio_total":0.0, **_nr})
            # Recompute y persistir
            for _r in _base:
                _c = float(_r.get("cantidad") or 0)
                _p = float(_r.get("precio_unitario") or 0)
                if _c > 0 and _p > 0:
                    _r["precio_total"] = round(_c * _p, 2)
            st.session_state[_base_key] = [dict(r) for r in _base]
            st.session_state.pop(_edit_key, None)

        auto_subtotal = round(sum(float(r.get("precio_total") or 0) for r in _base), 2)

        with st.expander("✏️ Editar productos en detalle"):
            st.data_editor(
                pd.DataFrame(_base),
                num_rows="dynamic",
                column_config={
                    "nombre":          st.column_config.TextColumn("Producto", required=True),
                    "cantidad":        st.column_config.NumberColumn("Cant.", format="%.3f"),
                    "unidad":          st.column_config.TextColumn("Unidad", width="small"),
                    "precio_unitario": st.column_config.NumberColumn("P. unit.", format="$%.2f"),
                    "precio_total":    st.column_config.NumberColumn("Total (auto)", format="$%.2f", disabled=True),
                    "cantidad_fisica": st.column_config.NumberColumn("Cant. física", format="%.0f"),
                    "unidad_fisica":   st.column_config.TextColumn("U. física", width="small"),
                },
                use_container_width=True,
                key=_edit_key,
            )
            if auto_subtotal > 0:
                st.caption(f"📐 Total calculado: **${auto_subtotal:,.2f}**")
        edited_prods = None  # ya no se usa en save; se lee de session_state

        # ── Guardar / Descartar ───────────────────────────────────────────────
        col_s, col_d = st.columns(2)
        with col_s:
            save = st.button("✅ Guardar", type="primary", use_container_width=True)
        with col_d:
            discard = st.button("🗑 Descartar", use_container_width=True)

        if discard:
            _clear_draft()
            st.rerun()

        if save:
            if not new_fecha.strip():
                st.error("La fecha es obligatoria.")
            else:
                prods_final = [dict(r) for r in st.session_state.get(_base_key, _norm_prods(prods))]

                # Garantizar precio_total correcto al guardar
                for p in prods_final:
                    c = float(p.get("cantidad") or 0)
                    u = float(p.get("precio_unitario") or 0)
                    if c > 0 and u > 0:
                        p["precio_total"] = round(c * u, 2)

                # Detectar si el usuario tocó el campo total manualmente
                orig_fecha   = (draft.get("fecha") or "").strip()
                orig_tipo    = draft.get("tipo", "factura_compra")
                orig_entidad = (draft.get("proveedor") or draft.get("cliente") or "").strip()
                orig_total   = float(draft.get("total") or 0)
                orig_folio   = str(draft.get("folio") or "").strip()
                user_touched_total = abs(new_total - orig_total) >= 0.01

                # Si el usuario no tocó el total, usar suma de productos (auto-corrección sin penalizar KPI)
                if auto_subtotal > 0 and not user_touched_total:
                    new_total = auto_subtotal

                def _prods_sig(ps):
                    return sorted((p.get("nombre",""), round(float(p.get("cantidad",0)),2)) for p in ps)

                sin_mod = (
                    new_tipo == orig_tipo
                    and new_fecha.strip() == orig_fecha
                    and new_entidad.strip() == orig_entidad
                    and not user_touched_total
                    and new_folio.strip() == orig_folio
                    and _prods_sig(prods_final) == _prods_sig(_norm_prods(prods))
                )

                final = {
                    "tipo":     new_tipo,
                    "fecha":    new_fecha,
                    "folio":    new_folio.strip() or None,
                    "total":    new_total,
                    "productos": prods_final,
                    "zona":     new_zona.strip(),
                    "pendiente": new_pendiente,
                    "sin_modificacion": sin_mod,
                    "medio":    st.session_state.get("cap_medio", ""),
                    ("proveedor" if new_tipo == "factura_compra" else "cliente"): new_entidad,
                }
                dup = check_duplicate(final)
                if dup:
                    st.warning(f"⚠️ Posible duplicado: {dup}")
                    if not st.checkbox("Guardar de todas formas"):
                        st.stop()
                save_document(final)

                # Aprender de correcciones: si el usuario cambió entidad o producto,
                # añadir la variante original como alias en el catálogo automáticamente
                if new_entidad.strip() and orig_entidad and new_entidad.strip().lower() != orig_entidad.lower():
                    campo = "proveedor" if new_tipo == "factura_compra" else "cliente"
                    learn_alias(orig_entidad, new_entidad.strip(), campo)
                orig_prods = _norm_prods(prods)
                if len(prods_final) == len(orig_prods):
                    for p_o, p_f in zip(orig_prods, prods_final):
                        n_o = (p_o.get("nombre") or "").strip()
                        n_f = (p_f.get("nombre") or "").strip()
                        if n_o and n_f and n_o.lower() != n_f.lower():
                            learn_alias(n_o, n_f, "producto")

                _clear_draft()
                st.success("✅ Guardado")
                st.rerun()

    # ── CAPTURA ───────────────────────────────────────────────────────────────
    else:
        key = st.session_state["upload_key"]

        # ──────────────────────────────────────────────────────────────────────
        # SECCIÓN FOTO / IMAGEN
        # ──────────────────────────────────────────────────────────────────────
        st.markdown("### 📷 Foto / Imagen")
        st.caption("La IA detecta automáticamente si es compra o venta y extrae todos los datos.")

        col_cam_btn, col_gal = st.columns(2)

        with col_cam_btn:
            if not st.session_state["camera_open"]:
                if st.button("📷 Abrir cámara", type="primary", use_container_width=True, key=f"btn_cam_{key}"):
                    st.session_state["camera_open"] = True
                    st.rerun()
            else:
                if st.button("✕ Cerrar cámara", use_container_width=True, key=f"close_cam_{key}"):
                    st.session_state["camera_open"] = False
                    st.rerun()

        with col_gal:
            up_img = st.file_uploader(
                "📁 Subir desde galería", type=["jpg","jpeg","png","webp"],
                key=f"img_{key}", label_visibility="collapsed",
            )
            if up_img:
                if st.button("⚡ Procesar imagen", type="primary", use_container_width=True, key=f"proc_img_{key}"):
                    with st.spinner("Procesando…"):
                        _process_and_draft(up_img.read(), up_img.name, medio="galeria")
                    st.rerun()

        if st.session_state["camera_open"]:
            camera_photo = st.camera_input("", key=f"cam_{key}", label_visibility="collapsed")
            if camera_photo:
                st.session_state["camera_open"] = False
                with st.spinner("Procesando…"):
                    _process_and_draft(camera_photo.getvalue(), "foto.jpg", medio="camara")
                st.rerun()

        st.divider()

        # ──────────────────────────────────────────────────────────────────────
        # OTRAS OPCIONES
        # ──────────────────────────────────────────────────────────────────────
        st.markdown("**Otras opciones**")
        modo = st.radio(
            "", ["🎙️ Audio", "✏️ Texto libre", "📊 Excel / CSV"],
            horizontal=True, key=f"modo_{key}", label_visibility="collapsed",
        )

        # ── AUDIO ─────────────────────────────────────────────────────────────
        if modo == "🎙️ Audio":
            st.caption("Graba directamente en el navegador o sube un archivo de audio.")
            st.caption("⏱️ Mínimo 3 segundos para que la transcripción funcione bien.")

            audio_tab_rec, audio_tab_file = st.tabs(["🎙 Grabar ahora", "📁 Subir archivo"])

            with audio_tab_rec:
                recorded = st.audio_input("Grabar nota de voz", label_visibility="collapsed",
                                          key=f"rec_{key}")
                if recorded:
                    if st.button("⚡ Procesar grabación", type="primary",
                                 use_container_width=True, key=f"proc_rec_{key}"):
                        with st.spinner("Transcribiendo…"):
                            transcript, data = _groq(extract_from_audio_auto, recorded.read(), "grabacion.wav")
                        if len(transcript.strip()) < 5:
                            st.warning("⚠️ Grabación muy corta o sin voz detectada. "
                                       "Graba al menos 3 segundos hablando claramente.")
                        else:
                            st.session_state["cap_draft"] = _normalize(data)
                            st.session_state["cap_transcript"] = transcript
                            st.session_state["cap_medio"] = "audio_grabacion"
                            st.rerun()

            with audio_tab_file:
                aud = st.file_uploader("Audio", type=["mp3","m4a","wav","ogg","webm"],
                                       key=f"aud_{key}", label_visibility="collapsed")
                if aud:
                    if st.button("⚡ Procesar audio", type="primary",
                                 use_container_width=True, key=f"proc_aud_{key}"):
                        with st.spinner("Transcribiendo…"):
                            transcript, data = _groq(extract_from_audio_auto, aud.read(), aud.name)
                        if len(transcript.strip()) < 5:
                            st.warning("⚠️ Audio muy corto o sin voz detectada. Verifica el archivo.")
                        else:
                            st.session_state["cap_draft"] = _normalize(data)
                            st.session_state["cap_transcript"] = transcript
                            st.session_state["cap_medio"] = "audio_archivo"
                            st.rerun()

        # ── TEXTO LIBRE ───────────────────────────────────────────────────────
        elif modo == "✏️ Texto libre":
            txt = st.text_area(
                "Descripción", height=120, key=f"txt_{key}", label_visibility="collapsed",
                placeholder='Ej: "Compré 10 kg de tomate a $25 el kg al proveedor García, total $250, el 05/06/2025"',
            )
            if txt.strip() and st.button("⚡ Procesar texto", type="primary"):
                with st.spinner("Extrayendo…"):
                    data = _normalize(_groq(extract_from_text_auto, txt))
                    st.session_state["cap_draft"] = data
                    st.session_state["cap_medio"] = "texto"
                st.rerun()

        # ── EXCEL / CSV ───────────────────────────────────────────────────────
        elif modo == "📊 Excel / CSV":
            tipo_xl = st.radio(
                "Contenido", ["Solo compras","Solo ventas","Mezcla (auto-detectar)"],
                horizontal=True, key="xl_tipo",
            )
            xl_map = {"Solo compras":"factura_compra","Solo ventas":"nota_venta","Mezcla (auto-detectar)":"auto"}
            xl = st.file_uploader("Excel/CSV", type=["xlsx","xls","csv"], key=f"xl_{key}", label_visibility="collapsed")
            if xl and st.button("⚡ Importar", type="primary"):
                with st.spinner("Interpretando archivo…"):
                    docs = extract_from_excel(xl.read(), xl.name, xl_map[tipo_xl])
                    saved = skipped = 0
                    for d in docs:
                        if check_duplicate(d):
                            skipped += 1
                        else:
                            d["medio"] = "excel"
                            d["sin_modificacion"] = True
                            save_document(d)
                            saved += 1
                st.success(f"✅ {saved} registros importados" + (f" · {skipped} omitidos por duplicado" if skipped else ""))
                st.session_state["upload_key"] += 1
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — REGISTROS
# ══════════════════════════════════════════════════════════════════════════════
with tab_reg:
    rows = get_all_documents()

    if not rows:
        st.info("Aún no hay registros.")
    else:
        c1, c2, c3 = st.columns([3,1,1])
        with c1:
            st.subheader(f"{len(rows)} documentos")
        with c2:
            st.download_button("⬇️ CSV", export_csv(), "pyme_registros.csv", "text/csv", use_container_width=True)
        with c3:
            if st.button("🗑 Limpiar", use_container_width=True):
                st.session_state["confirm_del"] = True

        if st.session_state.get("confirm_del"):
            st.warning("¿Borrar TODOS los registros?")
            ca, cb = st.columns(2)
            with ca:
                if st.button("Sí, borrar todo", type="primary"):
                    delete_all()
                    for k in ["confirm_del","agente_analisis","chat_history"]:
                        st.session_state.pop(k, None)
                    st.rerun()
            with cb:
                if st.button("Cancelar"):
                    st.session_state.pop("confirm_del", None)
                    st.rerun()

        search = st.text_input("🔍 Buscar proveedor / cliente", key="reg_search",
                               label_visibility="collapsed",
                               placeholder="🔍 Buscar proveedor / cliente…")

        df = pd.DataFrame(rows, columns=["ID","tipo","Fecha captura","Fecha doc",
                                          "Proveedor/Cliente","Folio","Total","Productos",
                                          "Zona","Pendiente","Sin_mod","Medio"])
        df["Tipo"]  = df["tipo"].map(TIPO_LABEL).fillna("Otro")
        df["Total"] = df["Total"].apply(lambda x: f"${x:,.2f}")

        def _get_alert(r):
            a = []
            if r["tipo"] == "nota_venta" and not str(r["Folio"]).strip():
                a.append("Sin folio")
            if not str(r["Fecha doc"]).strip():
                a.append("Sin fecha")
            if r["Pendiente"]:
                a.append("⏳ Pendiente")
            return " · ".join(a) if a else "✓"

        df["Alertas"] = df.apply(_get_alert, axis=1)

        if search:
            df = df[df["Proveedor/Cliente"].str.contains(search, case=False, na=False)]

        st.dataframe(
            df[["Tipo","Fecha doc","Proveedor/Cliente","Folio","Zona","Total","Alertas","Medio"]],
            use_container_width=True, hide_index=True,
        )

        with st.expander("🔍 Ver / Editar registro"):
            sel = st.selectbox("ID", [r[0] for r in rows], format_func=lambda x: f"ID {x}")
            row = next((r for r in rows if r[0] == sel), None)
            if row:
                st.caption(f"Capturado: {row[2]}  ·  Medio: {MEDIO_LABEL.get(row[11], row[11] or '—')}")
                if row[9]:
                    st.warning("⏳ Pendiente por confirmar")
                prods = json.loads(row[7] or "[]")
                if prods and not st.session_state.get("reg_edit_id") == sel:
                    st.dataframe(pd.DataFrame(_norm_prods(prods)), use_container_width=True, hide_index=True)

                col_ebtn, _ = st.columns([1, 3])
                with col_ebtn:
                    if st.session_state.get("reg_edit_id") != sel:
                        if st.button("✏️ Editar", key=f"edit_open_{sel}"):
                            st.session_state["reg_edit_id"] = sel
                            for k in ["re_prods_edit","re_prods_resolved","re_prods_sig"]:
                                st.session_state.pop(k, None)
                            st.rerun()
                    else:
                        if st.button("✕ Cancelar edición", key=f"edit_close_{sel}"):
                            st.session_state.pop("reg_edit_id", None)
                            st.rerun()

                if st.session_state.get("reg_edit_id") == sel:
                    st.markdown("---")
                    r_tipo    = row[1]
                    r_fecha   = row[3] or ""
                    r_entidad = row[4] or ""
                    r_folio   = row[5] or ""
                    r_total   = float(row[6] or 0)
                    r_zona    = row[8] or ""
                    r_pend    = bool(row[9])
                    r_prods   = json.loads(row[7] or "[]")

                    ec1, ec2 = st.columns(2)
                    with ec1:
                        e_tipo_lbl = st.selectbox("Tipo", list(TIPO_OPTIONS.keys()),
                            index=list(TIPO_OPTIONS.values()).index(r_tipo) if r_tipo in TIPO_OPTIONS.values() else 0,
                            key="re_tipo")
                        e_tipo = TIPO_OPTIONS[e_tipo_lbl]
                    with ec2:
                        e_fecha = st.text_input("Fecha del documento *", value=r_fecha, key="re_fecha")

                    ec3, ec4 = st.columns(2)
                    ent_lbl = "Proveedor" if e_tipo == "factura_compra" else "Cliente"
                    with ec3:
                        e_entidad = st.text_input(ent_lbl, value=r_entidad, key="re_entidad")
                    with ec4:
                        e_total = st.number_input("Total ($)", value=r_total, min_value=0.0, format="%.2f", key="re_total")

                    ec5, ec6 = st.columns(2)
                    with ec5:
                        e_folio = st.text_input("Folio", value=r_folio, key="re_folio")
                    with ec6:
                        if e_tipo in ("nota_venta", "venta_publico"):
                            e_zona = st.text_input("Zona / colonia", value=r_zona, key="re_zona")
                        else:
                            e_zona = ""
                    if e_tipo == "nota_venta" and not e_folio.strip():
                        e_pend = st.checkbox("⏳ Pendiente por confirmar", value=r_pend, key="re_pend")
                    else:
                        e_pend = False

                    # Productos con auto-cálculo (misma lógica, claves re_*)
                    _rek = "re_prods_edit"; _rbk = "re_prods_resolved"; _rsk = "re_prods_sig"
                    _rsig = json.dumps(r_prods, sort_keys=True, ensure_ascii=False)
                    if st.session_state.get(_rsk) != _rsig:
                        st.session_state[_rbk] = _norm_prods(r_prods)
                        st.session_state[_rsk]  = _rsig
                        st.session_state.pop(_rek, None)
                    _rdelta = st.session_state.get(_rek, {})
                    _rbase  = [dict(r) for r in st.session_state.get(_rbk, _norm_prods(r_prods))]
                    if _rdelta:
                        for _si, _ch in (_rdelta.get("edited_rows") or {}).items():
                            _i = int(_si)
                            if _i < len(_rbase): _rbase[_i].update(_ch)
                        for _di in sorted((_rdelta.get("deleted_rows") or []), reverse=True):
                            if _di < len(_rbase): _rbase.pop(_di)
                        for _nr in (_rdelta.get("added_rows") or []):
                            _rbase.append({"nombre":"","cantidad":0,"unidad":"","precio_unitario":0.0,"precio_total":0.0,**_nr})
                        for _r in _rbase:
                            _c = float(_r.get("cantidad") or 0); _p = float(_r.get("precio_unitario") or 0)
                            if _c > 0 and _p > 0: _r["precio_total"] = round(_c * _p, 2)
                        st.session_state[_rbk] = [dict(r) for r in _rbase]
                        st.session_state.pop(_rek, None)
                    re_auto_total = round(sum(float(r.get("precio_total") or 0) for r in _rbase), 2)
                    with st.expander("✏️ Productos", expanded=True):
                        st.data_editor(pd.DataFrame(_rbase), num_rows="dynamic",
                            column_config={
                                "nombre": st.column_config.TextColumn("Producto"),
                                "cantidad": st.column_config.NumberColumn("Cant.", format="%.3f"),
                                "unidad": st.column_config.TextColumn("Unidad", width="small"),
                                "precio_unitario": st.column_config.NumberColumn("P. unit.", format="$%.2f"),
                                "precio_total": st.column_config.NumberColumn("Total (auto)", format="$%.2f", disabled=True),
                            }, use_container_width=True, key=_rek)
                        if re_auto_total > 0:
                            st.caption(f"📐 Total calculado: **${re_auto_total:,.2f}**")

                    if st.button("💾 Guardar cambios", type="primary", key=f"edit_save_{sel}"):
                        if not e_fecha.strip():
                            st.error("La fecha es obligatoria.")
                        else:
                            ef_prods = [dict(r) for r in st.session_state.get(_rbk, _norm_prods(r_prods))]
                            for p in ef_prods:
                                c = float(p.get("cantidad") or 0); u = float(p.get("precio_unitario") or 0)
                                if c > 0 and u > 0: p["precio_total"] = round(c * u, 2)
                            e_total_f = re_auto_total if re_auto_total > 0 and abs(e_total - r_total) < 0.01 else e_total
                            update_document(sel, {
                                "tipo": e_tipo, "fecha": e_fecha,
                                ("proveedor" if e_tipo == "factura_compra" else "cliente"): e_entidad,
                                "folio": e_folio.strip() or None,
                                "total": e_total_f, "productos": ef_prods,
                                "zona": e_zona.strip(), "pendiente": e_pend,
                            })
                            st.session_state.pop("reg_edit_id", None)
                            for k in [_rek, _rbk, _rsk]: st.session_state.pop(k, None)
                            st.success("✅ Registro actualizado")
                            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AGENTE IA
# ══════════════════════════════════════════════════════════════════════════════
with tab_agent:
    all_rows = get_all_documents()

    if not all_rows:
        st.info("Captura documentos para que el agente pueda analizar tu negocio.")
    else:
        col_p, col_btn = st.columns([3,1])
        with col_p:
            periodo = st.selectbox(
                "Período",
                ["Todo el historial","Esta semana","Este mes","Últimos 30 días","Rango personalizado"],
                key="periodo",
                label_visibility="collapsed",
            )
        with col_btn:
            if st.button("🔄 Actualizar", use_container_width=True):
                st.session_state.pop("agente_analisis", None)
                st.session_state.pop("agente_period", None)

        today = date.today()
        start_str = end_str = None
        period_label = "historial completo"

        if periodo == "Esta semana":
            start_str = (today - timedelta(days=today.weekday())).strftime("%d/%m/%Y")
            period_label = "esta semana"
        elif periodo == "Este mes":
            start_str = today.replace(day=1).strftime("%d/%m/%Y")
            period_label = f"mes de {today.strftime('%B %Y')}"
        elif periodo == "Últimos 30 días":
            start_str = (today - timedelta(days=30)).strftime("%d/%m/%Y")
            period_label = "últimos 30 días"
        elif periodo == "Rango personalizado":
            ca, cb = st.columns(2)
            d_ini = ca.date_input("Desde", value=today - timedelta(days=30))
            d_fin = cb.date_input("Hasta", value=today)
            start_str = d_ini.strftime("%d/%m/%Y")
            end_str   = d_fin.strftime("%d/%m/%Y")
            period_label = f"{start_str} – {end_str}"

        filtered = get_filtered_documents(start_str, end_str)
        context  = build_context(filtered)

        period_key = periodo + str(start_str) + str(end_str)
        if st.session_state.get("agente_period") != period_key:
            st.session_state.pop("agente_analisis", None)
            st.session_state["agente_period"] = period_key

        st.caption(f"**{len(filtered)}** documentos · {period_label}")

        if not filtered:
            st.info("No hay registros para este período.")
        else:
            _cols = ["id","tipo","fecha_cap","fecha_doc","entidad","folio","total","productos",
                     "zona","pendiente","sin_modificacion","medio"]
            df_f = _parse_df_dates(pd.DataFrame(filtered, columns=_cols))

            c_tot = df_f[df_f["tipo"]=="factura_compra"]["total"].sum()
            v_tot = df_f[df_f["tipo"].isin(["nota_venta","venta_publico"])]["total"].sum()

            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("Total compras", f"${c_tot:,.2f}")
            cc2.metric("Total ventas",  f"${v_tot:,.2f}")
            if c_tot > 0 and v_tot > 0:
                cc3.metric("Margen", f"{((v_tot-c_tot)/v_tot*100):.1f}%")

            if len(filtered) >= 3:
                df_f["semana"] = df_f["fecha"].dt.to_period("W").dt.start_time
                df_f["Tipo"] = df_f["tipo"].map({"factura_compra":"Compras","nota_venta":"Ventas","venta_publico":"Ventas"})
                wk = df_f.dropna(subset=["semana"]).groupby(["semana","Tipo"])["total"].sum().reset_index()
                if not wk.empty:
                    fig_ag = px.bar(wk, x="semana", y="total", color="Tipo",
                                    color_discrete_map={"Compras":"#ef4444","Ventas":"#22c55e"},
                                    barmode="group", height=220,
                                    labels={"semana":"","total":"$"})
                    fig_ag.update_layout(margin=dict(t=10,b=10), legend_title_text="")
                    st.plotly_chart(fig_ag, use_container_width=True)

            st.subheader("🤖 Diagnóstico")
            if "agente_analisis" not in st.session_state:
                with st.spinner("Analizando…"):
                    st.session_state["agente_analisis"] = _groq(analyze_business, context, period_label)
            st.markdown(st.session_state["agente_analisis"])

            st.divider()
            st.subheader("💬 Pregunta")
            st.caption("Ej: ¿Cuánto gasté en tomate? · ¿Cuál es mi margen? · ¿Qué debería hacer esta semana?")

            if "chat_history" not in st.session_state:
                st.session_state["chat_history"] = []

            for msg in st.session_state["chat_history"]:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])

            if prompt := st.chat_input("Escribe tu pregunta…"):
                st.session_state["chat_history"].append({"role":"user","content":prompt})
                with st.chat_message("user"):
                    st.write(prompt)
                with st.chat_message("assistant"):
                    with st.spinner("…"):
                        reply = _groq(chat_with_agent, context, st.session_state["chat_history"][:-1], prompt)
                    st.write(reply)
                st.session_state["chat_history"].append({"role":"assistant","content":reply})

# ── TAB CATÁLOGO ──────────────────────────────────────────────────────────────

def _cat_render_section(tipo: str, get_fn, upsert_fn, delete_fn,
                        row_fn, field_keys: list[str]):
    """Renderiza una sección del catálogo: entrada por voz/texto + tabla + borrar."""
    key_draft  = f"cat_{tipo}_draft"
    key_audio  = f"cat_{tipo}_audio"

    # ── Entrada por voz o texto ───────────────────────────────────────────────
    inp_audio, inp_texto = st.tabs(["🎙 Voz", "⌨️ Texto"])

    with inp_audio:
        audio_val = st.audio_input("Graba lo que quieras registrar", key=key_audio)
        if audio_val and st.button("⚡ Procesar audio", key=f"cat_{tipo}_proc_audio", type="primary"):
            with st.spinner("Transcribiendo…"):
                transcript, _ = _groq(extract_from_audio_auto, audio_val.read(), "audio.ogg")
            with st.spinner("Extrayendo datos…"):
                st.session_state[key_draft] = _groq(extract_catalogo_entry, transcript, tipo)
            st.rerun()

    with inp_texto:
        hint = {
            "cliente":   "Ej: mi cliente Juan García, vive en el Centro, también lo conozco como Juanito",
            "proveedor": "Ej: proveedor Aceros del Norte, también les digo Aceros",
            "producto":  "Ej: varilla de 3/8, también la llamo varilla tres octavos, se vende por kg",
        }.get(tipo, "")
        txt_val = st.text_area("Describe el registro", placeholder=hint, key=f"cat_{tipo}_txt", height=80)
        if txt_val.strip() and st.button("⚡ Procesar texto", key=f"cat_{tipo}_proc_txt", type="primary"):
            with st.spinner("Extrayendo datos…"):
                st.session_state[key_draft] = _groq(extract_catalogo_entry, txt_val.strip(), tipo)
            st.rerun()

    # ── Preview editable ──────────────────────────────────────────────────────
    if key_draft in st.session_state:
        draft = st.session_state[key_draft]
        st.markdown("**Revisar antes de guardar:**")
        edited = {}
        for fk in field_keys:
            val = draft.get(fk, "")
            if isinstance(val, list):
                edited[fk] = [v.strip() for v in
                               st.text_input(fk.capitalize(), value=", ".join(val),
                                             key=f"cat_{tipo}_f_{fk}").split(",") if v.strip()]
            else:
                edited[fk] = st.text_input(fk.capitalize(), value=str(val or ""),
                                            key=f"cat_{tipo}_f_{fk}")

        sv, cl = st.columns(2)
        if sv.button("✅ Guardar", key=f"cat_{tipo}_save", type="primary"):
            if edited.get("nombre", "").strip():
                upsert_fn(**{k: v for k, v in edited.items()})
                del st.session_state[key_draft]
                st.success("Guardado.")
                st.rerun()
            else:
                st.error("El nombre es obligatorio.")
        if cl.button("🗑 Descartar", key=f"cat_{tipo}_discard"):
            del st.session_state[key_draft]
            st.rerun()

    # ── Tabla actual ──────────────────────────────────────────────────────────
    registros = get_fn()
    if registros:
        st.markdown("---")
        display_rows = []
        for i, r in enumerate(registros, 1):
            row = row_fn(r)
            row.pop("ID", None)
            display_rows.append({"#": i, **row})
        st.dataframe(pd.DataFrame(display_rows), hide_index=True, use_container_width=True)
        nombres = [r["nombre"] for r in registros]
        sel = st.selectbox("Eliminar registro", ["— ninguno —"] + nombres,
                           key=f"cat_{tipo}_del_sel")
        if sel != "— ninguno —" and st.button("🗑 Eliminar", key=f"cat_{tipo}_del_btn"):
            del_id = next(r["id"] for r in registros if r["nombre"] == sel)
            delete_fn(del_id)
            st.success(f"'{sel}' eliminado.")
            st.rerun()
    else:
        st.info("Sin registros aún.")


with tab_cat:
    st.subheader("📚 Catálogo del negocio")
    st.caption("Habla o escribe para registrar — la IA extrae los datos automáticamente.")

    cat_cli, cat_prov, cat_prod = st.tabs(["👥 Clientes", "🏭 Proveedores", "📦 Productos"])

    with cat_cli:
        _cat_render_section(
            tipo="cliente",
            get_fn=get_catalogo_clientes,
            upsert_fn=lambda nombre, alias, zona: upsert_cliente(nombre, alias, zona),
            delete_fn=delete_cliente,
            row_fn=lambda c: {"ID": c["id"], "Nombre": c["nombre"],
                               "Alias": ", ".join(c["alias"]), "Zona": c["zona"]},
            field_keys=["nombre", "alias", "zona"],
        )

    with cat_prov:
        _cat_render_section(
            tipo="proveedor",
            get_fn=get_catalogo_proveedores,
            upsert_fn=lambda nombre, alias, notas: upsert_proveedor(nombre, alias, notas),
            delete_fn=delete_proveedor,
            row_fn=lambda p: {"ID": p["id"], "Nombre": p["nombre"],
                               "Alias": ", ".join(p["alias"]), "Notas": p["notas"]},
            field_keys=["nombre", "alias", "notas"],
        )

    with cat_prod:
        _cat_render_section(
            tipo="producto",
            get_fn=get_catalogo_productos,
            upsert_fn=lambda nombre, variantes, unidad, notas: upsert_producto(nombre, variantes, unidad, notas),
            delete_fn=delete_producto,
            row_fn=lambda p: {"ID": p["id"], "Nombre": p["nombre"],
                               "Variantes": ", ".join(p["variantes"]),
                               "Unidad": p["unidad"], "Notas": p["notas"]},
            field_keys=["nombre", "variantes", "unidad", "notas"],
        )
