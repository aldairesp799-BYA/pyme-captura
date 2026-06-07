import io
import json
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st

# Streamlit Cloud secrets → env vars (must run before extractor import)
try:
    for _k in ["GROQ_API_KEY"]:
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

from database import (
    TIPO_LABEL,
    build_context,
    check_duplicate,
    delete_all,
    export_csv,
    get_all_documents,
    get_filtered_documents,
    init_db,
    save_document,
)
from extractor import (
    analyze_business,
    chat_with_agent,
    extract_from_audio,
    extract_from_excel,
    extract_from_image,
    extract_from_text,
)

st.set_page_config(page_title="PyME Captura", page_icon="📦", layout="wide")
init_db()

st.markdown("""
<style>
[data-testid="stMetric"] { background:#f8fafc; border-radius:10px; padding:14px; border:1px solid #e2e8f0; }
.stTabs [data-baseweb="tab"] { font-size:15px; font-weight:500; }
div[data-testid="stRadio"] > label { font-weight:600; }
</style>
""", unsafe_allow_html=True)

st.title("📦 PyME Captura")
st.caption("Registro automático de compras y ventas · impulsado por IA")

if "upload_key" not in st.session_state:
    st.session_state["upload_key"] = 0

tab_captura, tab_registros, tab_agente = st.tabs(["📸 Capturar", "📋 Registros", "🤖 Agente IA"])


# ── helpers ───────────────────────────────────────────────────────────────────

TIPO_OPTIONS = {
    "🛒 Factura de compra": "factura_compra",
    "📝 Nota de venta":     "nota_venta",
    "🏪 Venta al público (sin nota)": "venta_publico",
}

PROD_COLS = {
    "nombre":          st.column_config.TextColumn("Producto", required=True),
    "cantidad":        st.column_config.NumberColumn("Cantidad", min_value=0, format="%.2f"),
    "unidad":          st.column_config.TextColumn("Unidad (kg, pz, lt…)"),
    "precio_unitario": st.column_config.NumberColumn("Precio unit.", format="$%.2f", min_value=0.0),
    "precio_total":    st.column_config.NumberColumn("Total prod.", format="$%.2f", min_value=0.0),
}

EMPTY_PROD = {"nombre": "", "cantidad": 0, "unidad": "", "precio_unitario": 0.0, "precio_total": 0.0}


def _normalize_prods(prods: list) -> list:
    out = []
    for p in prods:
        out.append({
            "nombre":          p.get("nombre", ""),
            "cantidad":        p.get("cantidad", 0),
            "unidad":          p.get("unidad", ""),
            "precio_unitario": p.get("precio_unitario", 0.0),
            "precio_total":    p.get("precio_total", 0.0),
        })
    return out or [EMPTY_PROD.copy()]


def _show_review(draft: dict):
    """Render editable review form. Returns (confirmed, final_data) or (False, None)."""
    st.subheader("✏️ Revisa y corrige antes de guardar")

    # Doc type selector
    tipo_label = st.selectbox(
        "Tipo de documento",
        list(TIPO_OPTIONS.keys()),
        index=list(TIPO_OPTIONS.values()).index(draft.get("tipo", "factura_compra")),
        key="rev_tipo",
    )
    tipo = TIPO_OPTIONS[tipo_label]

    col1, col2, col3 = st.columns(3)
    with col1:
        entidad_label = "Proveedor" if tipo == "factura_compra" else "Cliente"
        entidad = st.text_input(entidad_label, value=draft.get("proveedor") or draft.get("cliente") or "", key="rev_entidad")
    with col2:
        fecha_val = draft.get("fecha") or ""
        if not fecha_val:
            st.warning("⚠️ No se detectó fecha en el documento.")
        fecha = st.text_input(
            "Fecha del documento (DD/MM/YYYY) *",
            value=fecha_val,
            placeholder=date.today().strftime("%d/%m/%Y"),
            key="rev_fecha",
            help="Usa la fecha que aparece en el documento, no de hoy",
        )
    with col3:
        folio = st.text_input("Folio", value=draft.get("folio") or "", key="rev_folio", disabled=(tipo == "venta_publico"))

    total = st.number_input("Total ($)", value=float(draft.get("total") or 0.0), min_value=0.0, format="%.2f", key="rev_total")

    st.markdown("**Productos** — puedes editar, agregar o eliminar filas:")
    prods_df = pd.DataFrame(_normalize_prods(draft.get("productos", [])))
    edited = st.data_editor(
        prods_df,
        num_rows="dynamic",
        column_config=PROD_COLS,
        use_container_width=True,
        key="rev_prods",
    )

    col_save, col_discard = st.columns([1, 1])
    with col_save:
        confirmed = st.button("✅ Confirmar y guardar", type="primary", use_container_width=True)
    with col_discard:
        discarded = st.button("🗑 Descartar", use_container_width=True)

    if discarded:
        return "discard", None

    if confirmed:
        if not fecha.strip():
            st.error("La fecha del documento es obligatoria. Ingresa cuándo se generó.")
            return "waiting", None
        final = {
            "tipo":   tipo,
            "fecha":  fecha,
            "folio":  folio if tipo != "venta_publico" else None,
            "total":  total,
            "productos": edited.fillna("").to_dict("records"),
        }
        if tipo == "factura_compra":
            final["proveedor"] = entidad
        else:
            final["cliente"] = entidad
        return "save", final

    return "waiting", None


# ── TAB 1: CAPTURAR ───────────────────────────────────────────────────────────
with tab_captura:

    # ── If a draft is waiting for review ──────────────────────────────────────
    if "cap_draft" in st.session_state:
        action, final = _show_review(st.session_state["cap_draft"])

        if action == "discard":
            del st.session_state["cap_draft"]
            st.session_state["upload_key"] += 1
            st.rerun()

        elif action == "save":
            dup = check_duplicate(final)
            if dup:
                st.warning(f"⚠️ Posible duplicado: {dup}. Revisa si ya tienes este registro.")
                if not st.checkbox("Guardar de todas formas", key="force_save"):
                    st.stop()
            save_document(final)
            del st.session_state["cap_draft"]
            st.session_state["cap_res"] = final
            st.session_state["upload_key"] += 1
            st.rerun()

    # ── Input form ────────────────────────────────────────────────────────────
    elif "cap_res" not in st.session_state:
        col_opts, col_main = st.columns([1, 2], gap="large")

        with col_opts:
            tipo_label = st.radio("Tipo de documento", list(TIPO_OPTIONS.keys()))
            doc_type = TIPO_OPTIONS[tipo_label]
            st.divider()
            modo = st.radio("Método de captura", ["📷 Foto", "🎙️ Audio", "✏️ Texto libre", "📊 Excel / CSV"])

        with col_main:
            key = st.session_state["upload_key"]

            if modo == "📷 Foto":
                uploaded = st.file_uploader("Sube o toma foto del documento", type=["jpg", "jpeg", "png", "webp"], key=f"img_{key}")
                if uploaded:
                    raw = uploaded.read()
                    st.session_state.update({"_bytes": raw, "_name": uploaded.name})
                    st.image(raw, width=360)
                if st.session_state.get("_bytes"):
                    if st.button("⚡ Procesar foto", type="primary"):
                        with st.spinner("Leyendo documento… (10-20 seg)"):
                            f = io.BytesIO(st.session_state["_bytes"])
                            f.name = st.session_state.get("_name", "imagen.jpg")
                            draft = extract_from_image(f, doc_type)
                            draft["tipo"] = doc_type
                            st.session_state["cap_draft"] = draft
                        st.rerun()

            elif modo == "🎙️ Audio":
                st.info("Graba una nota de voz en tu celular y súbela. Describe productos, cantidades, precios y proveedor/cliente.")
                audio_file = st.file_uploader("Archivo de audio", type=["mp3", "m4a", "wav", "ogg", "webm"], key=f"aud_{key}")
                if audio_file and st.button("⚡ Procesar audio", type="primary"):
                    with st.spinner("Transcribiendo y extrayendo datos…"):
                        transcript, draft = extract_from_audio(audio_file.read(), audio_file.name, doc_type)
                        draft["tipo"] = doc_type
                        st.session_state["cap_draft"] = draft
                        st.session_state["cap_transcript"] = transcript
                    st.rerun()

                if st.session_state.get("cap_transcript"):
                    with st.expander("📝 Ver transcripción"):
                        st.write(st.session_state["cap_transcript"])

            elif modo == "✏️ Texto libre":
                texto = st.text_area(
                    "Describe la operación con tus propias palabras",
                    height=140,
                    placeholder='Ej: "Compré 10 kg de tomate a $25 el kg y 5 lt de aceite a $40, total $290. Proveedor Abarrotes García, hoy."',
                    key=f"txt_{key}",
                )
                if texto.strip() and st.button("⚡ Procesar texto", type="primary"):
                    with st.spinner("Extrayendo datos…"):
                        draft = extract_from_text(texto, doc_type)
                        draft["tipo"] = doc_type
                        st.session_state["cap_draft"] = draft
                    st.rerun()

            elif modo == "📊 Excel / CSV":
                tipo_xl = st.radio(
                    "¿Qué contiene el archivo?",
                    ["Solo compras", "Solo ventas", "Mezcla de compras y ventas (auto-detectar)"],
                    horizontal=True,
                    key="xl_tipo",
                )
                xl_map = {
                    "Solo compras": "factura_compra",
                    "Solo ventas": "nota_venta",
                    "Mezcla de compras y ventas (auto-detectar)": "auto",
                }
                xl_doc_type = xl_map[tipo_xl]
                xl_file = st.file_uploader("Archivo Excel o CSV", type=["xlsx", "xls", "csv"], key=f"xl_{key}")

                if xl_file and st.button("⚡ Importar archivo", type="primary"):
                    with st.spinner("Interpretando archivo…"):
                        docs = extract_from_excel(xl_file.read(), xl_file.name, xl_doc_type)
                        saved, skipped = 0, 0
                        for d in docs:
                            dup = check_duplicate(d)
                            if dup:
                                skipped += 1
                            else:
                                save_document(d)
                                saved += 1
                        st.session_state["cap_res"] = {"_multi": saved, "_skipped": skipped}
                    st.rerun()

    # ── Result screen ─────────────────────────────────────────────────────────
    elif "cap_res" in st.session_state:
        res = st.session_state["cap_res"]
        st.divider()

        if res.get("_multi") is not None:
            st.success(f"✅ {res['_multi']} registros importados.")
            if res.get("_skipped"):
                st.info(f"Se omitieron {res['_skipped']} registros por posible duplicado.")
        else:
            tipo = res.get("tipo", "")
            if tipo == "factura_compra":
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Proveedor", res.get("proveedor") or "—")
                c2.metric("Fecha", res.get("fecha") or "—")
                c3.metric("Folio", res.get("folio") or "—")
                c4.metric("Total", f"${res.get('total') or 0:,.2f}")
            elif tipo == "nota_venta":
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Cliente", res.get("cliente") or "—")
                c2.metric("Fecha", res.get("fecha") or "—")
                c3.metric("Folio", res.get("folio") or "—")
                c4.metric("Total", f"${res.get('total') or 0:,.2f}")
            else:
                c1, c2, c3 = st.columns(3)
                c1.metric("Tipo", "Venta al público")
                c2.metric("Fecha", res.get("fecha") or "—")
                c3.metric("Total", f"${res.get('total') or 0:,.2f}")

            prods = res.get("productos", [])
            if prods:
                st.dataframe(pd.DataFrame(_normalize_prods(prods)), use_container_width=True, hide_index=True)

        if st.button("📸 Capturar nuevo documento"):
            for k in ["cap_res", "cap_transcript", "_bytes", "_name"]:
                st.session_state.pop(k, None)
            st.session_state["upload_key"] += 1
            st.rerun()


# ── TAB 2: REGISTROS ──────────────────────────────────────────────────────────
with tab_registros:
    rows = get_all_documents()

    if not rows:
        st.info("Aún no hay registros. Ve a **Capturar** para agregar el primero.")
    else:
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            st.subheader(f"{len(rows)} documentos capturados")
        with c2:
            csv_bytes = export_csv()
            st.download_button("⬇️ Exportar CSV", csv_bytes, "pyme_registros.csv", "text/csv", use_container_width=True)
        with c3:
            if st.button("🗑 Limpiar todo", use_container_width=True):
                st.session_state["confirm_delete"] = True

        if st.session_state.get("confirm_delete"):
            st.warning("⚠️ Esto borrará TODOS los registros. ¿Seguro?")
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("Sí, borrar todo", type="primary"):
                    delete_all()
                    for k in ["confirm_delete", "agente_analisis", "chat_history"]:
                        st.session_state.pop(k, None)
                    st.rerun()
            with col_no:
                if st.button("Cancelar"):
                    st.session_state.pop("confirm_delete", None)
                    st.rerun()

        df = pd.DataFrame(rows, columns=["ID", "Tipo", "Fecha captura", "Fecha doc", "Proveedor/Cliente", "Folio", "Total", "Productos"])
        df["Tipo"] = df["Tipo"].map(TIPO_LABEL).fillna("Desconocido")
        df["Total"] = df["Total"].apply(lambda x: f"${x:,.2f}")

        st.dataframe(
            df[["Tipo", "Fecha doc", "Fecha captura", "Proveedor/Cliente", "Folio", "Total"]],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("🔍 Ver detalle de un registro"):
            selected_id = st.selectbox("Seleccionar por ID", [r[0] for r in rows], format_func=lambda x: f"ID {x}")
            row = next((r for r in rows if r[0] == selected_id), None)
            if row:
                prods = json.loads(row[7])
                if prods:
                    st.dataframe(pd.DataFrame(_normalize_prods(prods)), use_container_width=True, hide_index=True)
                else:
                    st.write("Sin detalle de productos.")


# ── TAB 3: AGENTE IA ──────────────────────────────────────────────────────────
with tab_agente:
    all_rows = get_all_documents()

    if not all_rows:
        st.info("Captura al menos un documento para que el agente pueda analizar tu negocio.")
    else:
        # ── Period filter ──────────────────────────────────────────────────────
        st.subheader("🤖 Diagnóstico de tu negocio")

        col_f1, col_f2, col_f3, col_f4 = st.columns([1, 1, 1, 1])
        with col_f1:
            periodo = st.selectbox("Período", ["Todo el historial", "Esta semana", "Este mes", "Últimos 30 días", "Rango personalizado"], key="periodo")

        today = date.today()
        start_str, end_str, period_label = None, None, "historial completo"

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
            with col_f2:
                d_ini = st.date_input("Desde", value=today - timedelta(days=30), key="d_ini")
            with col_f3:
                d_fin = st.date_input("Hasta", value=today, key="d_fin")
            start_str = d_ini.strftime("%d/%m/%Y")
            end_str = d_fin.strftime("%d/%m/%Y")
            period_label = f"{start_str} – {end_str}"

        filtered_rows = get_filtered_documents(start_str, end_str)
        context = build_context(filtered_rows)

        with col_f4:
            if st.button("🔄 Actualizar análisis", use_container_width=True):
                st.session_state.pop("agente_analisis", None)
                st.session_state.pop("agente_period", None)

        # Invalidate cache if period changed
        if st.session_state.get("agente_period") != periodo + str(start_str) + str(end_str):
            st.session_state.pop("agente_analisis", None)
            st.session_state["agente_period"] = periodo + str(start_str) + str(end_str)

        st.caption(f"Analizando **{len(filtered_rows)}** documentos · {period_label}")

        if not filtered_rows:
            st.info("No hay registros para el período seleccionado.")
        else:
            if "agente_analisis" not in st.session_state:
                with st.spinner("El agente está analizando tus datos…"):
                    st.session_state["agente_analisis"] = analyze_business(context, period_label)

            st.markdown(st.session_state["agente_analisis"])

            st.divider()
            st.subheader("💬 Pregúntale al agente")
            st.caption("Ejemplos: ¿Cuánto gasté en tomate? ¿Cuál es mi margen? ¿Qué producto me deja más?")

            if "chat_history" not in st.session_state:
                st.session_state["chat_history"] = []

            for msg in st.session_state["chat_history"]:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])

            if prompt := st.chat_input("Escribe tu pregunta…"):
                st.session_state["chat_history"].append({"role": "user", "content": prompt})
                with st.chat_message("user"):
                    st.write(prompt)
                with st.chat_message("assistant"):
                    with st.spinner("Pensando…"):
                        reply = chat_with_agent(context, st.session_state["chat_history"][:-1], prompt)
                    st.write(reply)
                st.session_state["chat_history"].append({"role": "assistant", "content": reply})
