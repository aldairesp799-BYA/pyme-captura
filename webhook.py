"""
WhatsApp webhook — PyME Captura
Deploy en Railway. Recibe mensajes de Twilio y procesa con IA.
"""
import os
from datetime import date

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv(override=False)  # Railway/producción: las vars del sistema siempre ganan

# Secrets bridge para Railway / Render
# (en local usa .env, en la nube las variables de entorno del servicio)

from database import init_db, save_document
from extractor import extract_from_audio_auto, extract_from_image_auto, extract_from_text_auto

app = FastAPI(title="PyME Captura — WhatsApp Webhook")
init_db()

# Estado de conversación por número de teléfono (en memoria)
_pending: dict[str, dict] = {}

CONFIRM = {"si", "sí", "yes", "ok", "s", "dale", "va", "correcto", "listo", "1", "✅"}
CANCEL  = {"no", "n", "cancelar", "cancel", "2", "❌"}


@app.get("/")
def health():
    return {"status": "ok", "service": "PyME Captura WhatsApp"}


@app.post("/whatsapp")
async def whatsapp(
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: int = Form(0),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None),
):
    resp = MessagingResponse()
    phone = From
    body_lower = Body.strip().lower()

    # ── Confirmación pendiente ─────────────────────────────────────────────────
    if phone in _pending:
        if body_lower in CONFIRM:
            data = _pending.pop(phone)["data"]
            # Si la fecha llegó vacía, usar hoy
            if not data.get("fecha"):
                data["fecha"] = date.today().strftime("%d/%m/%Y")
            save_document(data)
            tipo = {"factura_compra": "Compra", "nota_venta": "Venta", "venta_publico": "Venta"}.get(data.get("tipo", ""), "Documento")
            total = data.get("total") or 0
            resp.message(f"✅ {tipo} registrada — ${total:,.2f}\nYa puedes verla en el dashboard.")
        elif body_lower in CANCEL:
            _pending.pop(phone)
            resp.message("❌ Cancelado. Manda el documento de nuevo cuando quieras.")
        else:
            resp.message("Responde *SÍ* para guardar ✅ o *NO* para cancelar ❌")
        return _xml(resp)

    # ── Nuevo documento ────────────────────────────────────────────────────────
    try:
        data = None

        if NumMedia and NumMedia > 0 and MediaUrl0:
            media_bytes = await _download(MediaUrl0)
            ct = MediaContentType0 or ""

            if "image" in ct:
                data = extract_from_image_auto(media_bytes)

            elif "audio" in ct or "ogg" in ct or "mpeg" in ct:
                ext = "ogg" if "ogg" in ct else "mp3"
                _, data = extract_from_audio_auto(media_bytes, f"audio.{ext}")

            else:
                resp.message("📎 Tipo de archivo no soportado. Manda una foto 📷 o nota de voz 🎙️")
                return _xml(resp)

        elif Body.strip():
            # Primer mensaje puede ser un saludo
            if body_lower in ("hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"):
                resp.message(
                    "👋 ¡Hola! Soy tu asistente de registros.\n\n"
                    "Mándame:\n"
                    "📷 *Foto* de una factura o nota de venta\n"
                    "🎙️ *Nota de voz* describiendo la operación\n"
                    "✏️ *Texto* con los detalles\n\n"
                    "Lo registro automáticamente para ti."
                )
                return _xml(resp)
            data = extract_from_text_auto(Body.strip())

        else:
            resp.message(
                "📱 Manda una *foto* de tu factura/nota, una *nota de voz*, "
                "o descríbela por texto y la registro automáticamente."
            )
            return _xml(resp)

        _pending[phone] = {"data": data}
        resp.message(_format_confirm(data))

    except Exception as e:
        resp.message(
            f"❌ No pude leer el documento. Intenta con una foto más clara.\n"
            f"Detalle: {str(e)[:100]}"
        )

    return _xml(resp)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _xml(resp: MessagingResponse) -> PlainTextResponse:
    return PlainTextResponse(str(resp), media_type="text/xml")


async def _download(url: str) -> bytes:
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        raise ValueError("TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN no están configurados en las variables de entorno")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, auth=(sid, token))
        r.raise_for_status()
        return r.content


def _format_confirm(data: dict) -> str:
    tipo = data.get("tipo", "")
    labels = {"factura_compra": "🛒 Compra", "nota_venta": "📝 Venta", "venta_publico": "🏪 Venta al público"}
    tipo_label = labels.get(tipo, "📄 Documento")

    entidad = data.get("proveedor") or data.get("cliente") or ""
    fecha   = data.get("fecha") or f"⚠️ no detectada (se usará hoy: {date.today().strftime('%d/%m/%Y')})"
    total   = data.get("total") or 0
    prods   = data.get("productos") or []

    lines = [f"📋 *{tipo_label}*"]
    if entidad:
        key = "Proveedor" if tipo == "factura_compra" else "Cliente"
        lines.append(f"{key}: {entidad}")
    lines.append(f"Fecha: {fecha}")
    if data.get("folio"):
        lines.append(f"Folio: {data['folio']}")
    lines.append(f"Total: ${total:,.2f}")

    if prods:
        lines.append("\n*Productos:*")
        for p in prods[:6]:
            n  = p.get("nombre", "")
            c  = p.get("cantidad", 0)
            u  = p.get("unidad", "")
            pt = p.get("precio_total", 0)
            lines.append(f"• {n} {c}{' ' + u if u else ''} — ${pt:,.2f}")
        if len(prods) > 6:
            lines.append(f"  (+{len(prods) - 6} más)")

    lines.append("\n¿Correcto? *SÍ* para guardar ✅  /  *NO* para cancelar ❌")
    return "\n".join(lines)
