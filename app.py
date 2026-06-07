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
    get_filtered_documents, init_db, save_document,
)
from extractor import (
    analyze_business, chat_with_agent,
    extract_from_audio_auto, extract_from_excel,
    extract_from_image_auto, extract_from_text_auto,
)

st.set_page_config(page_title="PyME Captura", page_icon="📦", layout="centered")
init_db()

st.markdown("""
<style>
[data-testid="stMetric"] {
    background:#f8fafc; border-radius:12px; padding:14px;
    border:1px solid #e2e8f0; text-align:center;
}
.stTabs [data-baseweb="tab"] { font-size:15px; font-weight:600; }
[data-testid="stCameraInput"] label { font-size:18px; font-weight:600; }
div[data-testid="stVerticalBlock"] > div:has(> [data-testid="stButton"]) button[kind="primary"] {
    height:64px; font-size:18px;
}
/* Cámara más grande en móvil */
[data-testid="stCameraInput"] > div { border-radius:16px; overflow:hidden; }
[data-testid="stCameraInput"] video { min-height:260px; object-fit:cover; }
[data-testid="stCameraInput"] button {
    height:56px !important; font-size:17px !important;
    border-radius:12px !important;
}
</style>
""", unsafe_allow_html=True)

if "upload_key" not in st.session_state:
    st.session_state["upload_key"] = 0

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


def _process_and_draft(raw_bytes: bytes, filename: str = "imagen.jpg"):
    data = extract_from_image_auto(raw_bytes)
    st.session_state["cap_draft"] = data
    st.session_state.pop("cap_mode", None)


# ── TABS ──────────────────────────────────────────────────────────────────────
st.title("📦 PyME Captura")
tab_cap, tab_home, tab_reg, tab_agent = st.tabs(["📸 Capturar", "📊 Inicio", "📋 Registros", "🤖 Agente IA"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — INICIO / DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab_home:
    df_all = _parse_df_dates(get_all_as_df())

    if df_all.empty:
        st.info("Aún no hay registros. Ve a la pestaña **📸 Capturar** para empezar.")
    else:
        today = pd.Timestamp(date.today())
        week_start = today - pd.Timedelta(days=today.dayofweek)
        month_start = today.replace(day=1)

        compras = df_all[df_all["tipo"] == "factura_compra"]
        ventas  = df_all[df_all["tipo"].isin(["nota_venta","venta_publico"])]

        # ── Métricas rápidas ──────────────────────────────────────────────────────
        today_norm      = today.normalize()
        prev_week_start = week_start - pd.Timedelta(weeks=1)

        c_mes  = compras[compras["fecha"] >= month_start]["total"].sum()
        v_mes  = ventas[ventas["fecha"] >= month_start]["total"].sum()
        c_sem  = compras[compras["fecha"] >= week_start]["total"].sum()
        v_sem  = ventas[ventas["fecha"] >= week_start]["total"].sum()
        c_hoy  = compras[compras["fecha"].dt.normalize() == today_norm]["total"].sum()
        v_hoy  = ventas[ventas["fecha"].dt.normalize() == today_norm]["total"].sum()

        # Hoy (compacto)
        hoy_txt = f"**Hoy:** Compras ${c_hoy:,.0f}  ·  Ventas ${v_hoy:,.0f}"
        if c_hoy == 0 and v_hoy == 0:
            hoy_txt += "  —  sin capturas todavía"
        st.caption(hoy_txt)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📥 Compras (mes)",    f"${c_mes:,.0f}")
        c2.metric("📤 Ventas (mes)",     f"${v_mes:,.0f}")
        c3.metric("📥 Compras (semana)", f"${c_sem:,.0f}")
        c4.metric("📤 Ventas (semana)",  f"${v_sem:,.0f}")

        # ── Alertas automáticas ───────────────────────────────────────────────────
        c_prev = compras[(compras["fecha"] >= prev_week_start) & (compras["fecha"] < week_start)]["total"].sum()
        v_prev = ventas[(ventas["fecha"] >= prev_week_start) & (ventas["fecha"] < week_start)]["total"].sum()

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

        # ── Gráfica semanal ───────────────────────────────────────────────────────
        st.subheader("Actividad semanal")
        df_w = df_all[df_all["fecha"].notna()].copy()
        df_w["semana"] = df_w["fecha"].dt.to_period("W").dt.start_time
        df_w["Tipo"] = df_w["tipo"].map({"factura_compra":"Compras","nota_venta":"Ventas","venta_publico":"Ventas"})

        weekly = df_w.groupby(["semana","Tipo"])["total"].sum().reset_index()
        weekly = weekly[weekly["semana"] >= (today - pd.Timedelta(weeks=8))]

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

        # ── Top proveedores y productos ───────────────────────────────────────────
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
                    fig2 = px.bar(
                        top_prov.sort_values("Total ($)"),
                        x="Total ($)", y="Proveedor", orientation="h",
                        color_discrete_sequence=["#ef4444"],
                    )
                    fig2.update_layout(margin=dict(t=10,b=10), showlegend=False)
                    st.plotly_chart(fig2, use_container_width=True)
                else:
                    st.caption("Sin datos de proveedores.")
            else:
                st.caption("Sin compras registradas.")

        with col_b:
            st.subheader("Top productos")
            all_p = []
            for _, row in compras.iterrows():
                try:
                    for p in json.loads(row["productos"]):
                        if p.get("nombre"):
                            all_p.append({"Producto": p["nombre"], "Gasto": p.get("precio_total", 0)})
                except Exception:
                    pass
            if all_p:
                df_p = pd.DataFrame(all_p).groupby("Producto")["Gasto"].sum().nlargest(5).reset_index()
                fig3 = px.bar(
                    df_p.sort_values("Gasto"),
                    x="Gasto", y="Producto", orientation="h",
                    color_discrete_sequence=["#f97316"],
                )
                fig3.update_layout(margin=dict(t=10,b=10), showlegend=False)
                st.plotly_chart(fig3, use_container_width=True)
            else:
                st.caption("Sin detalle de productos.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — CAPTURAR
# ══════════════════════════════════════════════════════════════════════════════
with tab_cap:

    # ── REVISIÓN RÁPIDA ───────────────────────────────────────────────────────
    if "cap_draft" in st.session_state:
        draft = st.session_state["cap_draft"]
        tipo  = draft.get("tipo", "factura_compra")
        total = draft.get("total") or 0
        fecha = draft.get("fecha") or ""
        entidad = draft.get("proveedor") or draft.get("cliente") or ""
        prods = draft.get("productos") or []

        # ── Vista rápida ──────────────────────────────────────────────────────
        tipo_label = {
            "factura_compra": "🛒 Compra",
            "nota_venta":     "📝 Venta",
            "venta_publico":  "🏪 Venta pública",
        }.get(tipo, "📄 Documento")

        st.subheader(f"{tipo_label} · ${total:,.2f}")

        if not fecha:
            st.warning("⚠️ No se detectó fecha. Ingresa cuándo fue.")

        # Campos esenciales editables (compactos)
        col1, col2 = st.columns(2)
        with col1:
            new_tipo_lbl = st.selectbox(
                "Tipo", list(TIPO_OPTIONS.keys()),
                index=list(TIPO_OPTIONS.values()).index(tipo),
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
        new_entidad = st.text_input(ent_label, value=entidad, key="qr_entidad")
        new_total   = st.number_input("Total ($)", value=float(total), min_value=0.0, format="%.2f", key="qr_total")

        # Productos en vista compacta
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

        # Edición completa (opcional)
        edited_prods = None
        with st.expander("✏️ Editar productos en detalle"):
            edited_prods = st.data_editor(
                pd.DataFrame(_norm_prods(prods)),
                num_rows="dynamic",
                column_config=PROD_COLS,
                use_container_width=True,
                key="qr_prods_edit",
            )

        # Botones
        col_s, col_d = st.columns(2)
        with col_s:
            save = st.button("✅ Guardar", type="primary", use_container_width=True)
        with col_d:
            discard = st.button("🗑 Descartar", use_container_width=True)

        if discard:
            for k in ["cap_draft","_bytes","_name","cap_transcript"]:
                st.session_state.pop(k, None)
            st.session_state["upload_key"] += 1
            st.rerun()

        if save:
            if not new_fecha.strip():
                st.error("La fecha es obligatoria.")
            else:
                prods_final = edited_prods.fillna("").to_dict("records") if edited_prods is not None else _norm_prods(prods)
                final = {
                    "tipo":  new_tipo,
                    "fecha": new_fecha,
                    "folio": draft.get("folio"),
                    "total": new_total,
                    "productos": prods_final,
                    ("proveedor" if new_tipo == "factura_compra" else "cliente"): new_entidad,
                }
                dup = check_duplicate(final)
                if dup:
                    st.warning(f"⚠️ Posible duplicado: {dup}")
                    if not st.checkbox("Guardar de todas formas"):
                        st.stop()
                save_document(final)
                for k in ["cap_draft","_bytes","_name","cap_transcript","agente_analisis","agente_period"]:
                    st.session_state.pop(k, None)
                st.session_state["upload_key"] += 1
                st.success("✅ Guardado")
                st.rerun()

    # ── CAPTURA ───────────────────────────────────────────────────────────────
    else:
        key = st.session_state["upload_key"]

        st.markdown("### 📷 Toma o sube una foto")
        st.caption("La IA detecta automáticamente si es compra o venta y extrae todos los datos.")

        # Cámara directa (funciona en móvil)
        camera_photo = st.camera_input("Abrir cámara", key=f"cam_{key}", label_visibility="collapsed")

        if camera_photo:
            with st.spinner("Procesando…"):
                _process_and_draft(camera_photo.getvalue(), "foto.jpg")
            st.rerun()

        st.divider()
        st.markdown("**Otras opciones**")

        modo = st.radio(
            "", ["📁 Subir imagen", "🎙️ Audio", "✏️ Texto libre", "📊 Excel / CSV"],
            horizontal=True, key=f"modo_{key}", label_visibility="collapsed",
        )

        if modo == "📁 Subir imagen":
            up = st.file_uploader("Imagen", type=["jpg","jpeg","png","webp"], key=f"img_{key}", label_visibility="collapsed")
            if up and st.button("⚡ Procesar imagen", type="primary"):
                with st.spinner("Procesando…"):
                    _process_and_draft(up.read(), up.name)
                st.rerun()

        elif modo == "🎙️ Audio":
            st.caption("Graba una nota de voz en tu celular y súbela.")
            aud = st.file_uploader("Audio", type=["mp3","m4a","wav","ogg","webm"], key=f"aud_{key}", label_visibility="collapsed")
            if aud and st.button("⚡ Procesar audio", type="primary"):
                with st.spinner("Transcribiendo…"):
                    transcript, data = extract_from_audio_auto(aud.read(), aud.name)
                    st.session_state["cap_draft"] = data
                    st.session_state["cap_transcript"] = transcript
                st.rerun()
            if st.session_state.get("cap_transcript"):
                with st.expander("📝 Transcripción"):
                    st.write(st.session_state["cap_transcript"])

        elif modo == "✏️ Texto libre":
            txt = st.text_area(
                "Descripción", height=120, key=f"txt_{key}", label_visibility="collapsed",
                placeholder='Ej: "Compré 10 kg de tomate a $25 el kg al proveedor García, total $250, el 05/06/2025"',
            )
            if txt.strip() and st.button("⚡ Procesar texto", type="primary"):
                with st.spinner("Extrayendo…"):
                    st.session_state["cap_draft"] = extract_from_text_auto(txt)
                st.rerun()

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

        # Búsqueda rápida
        search = st.text_input("🔍 Buscar proveedor / cliente", key="reg_search", label_visibility="collapsed",
                               placeholder="🔍 Buscar proveedor / cliente…")

        df = pd.DataFrame(rows, columns=["ID","tipo","Fecha captura","Fecha doc","Proveedor/Cliente","Folio","Total","Productos"])
        df["Tipo"] = df["tipo"].map(TIPO_LABEL).fillna("Otro")
        df["Total"] = df["Total"].apply(lambda x: f"${x:,.2f}")

        if search:
            df = df[df["Proveedor/Cliente"].str.contains(search, case=False, na=False)]

        st.dataframe(
            df[["Tipo","Fecha doc","Fecha captura","Proveedor/Cliente","Folio","Total"]],
            use_container_width=True, hide_index=True,
        )

        with st.expander("🔍 Ver detalle"):
            sel = st.selectbox("ID", [r[0] for r in rows], format_func=lambda x: f"ID {x}")
            row = next((r for r in rows if r[0] == sel), None)
            if row:
                prods = json.loads(row[7])
                if prods:
                    st.dataframe(pd.DataFrame(_norm_prods(prods)), use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AGENTE IA
# ══════════════════════════════════════════════════════════════════════════════
with tab_agent:
    all_rows = get_all_documents()

    if not all_rows:
        st.info("Captura documentos para que el agente pueda analizar tu negocio.")
    else:
        # ── Filtro de período ─────────────────────────────────────────────────
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

        # Invalidar caché si cambió período
        period_key = periodo + str(start_str) + str(end_str)
        if st.session_state.get("agente_period") != period_key:
            st.session_state.pop("agente_analisis", None)
            st.session_state["agente_period"] = period_key

        st.caption(f"**{len(filtered)}** documentos · {period_label}")

        if not filtered:
            st.info("No hay registros para este período.")
        else:
            # ── Mini-gráficas del período ─────────────────────────────────────
            df_f = _parse_df_dates(
                pd.DataFrame(filtered, columns=["id","tipo","fecha_cap","fecha_doc","entidad","folio","total","productos"])
            )
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

            # ── Análisis IA ───────────────────────────────────────────────────
            st.subheader("🤖 Diagnóstico")
            if "agente_analisis" not in st.session_state:
                with st.spinner("Analizando…"):
                    st.session_state["agente_analisis"] = analyze_business(context, period_label)
            st.markdown(st.session_state["agente_analisis"])

            # ── Chat ──────────────────────────────────────────────────────────
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
                        reply = chat_with_agent(context, st.session_state["chat_history"][:-1], prompt)
                    st.write(reply)
                st.session_state["chat_history"].append({"role":"assistant","content":reply})
