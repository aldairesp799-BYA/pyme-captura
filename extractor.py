import base64
import io
import json
import os

from groq import Groq
from PIL import Image
import pandas as pd

_client = None

def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client

TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
AUDIO_MODEL = "whisper-large-v3"

_PROD = '{"nombre":"...","cantidad":0,"unidad":"pz/kg/lt/caja/etc","precio_unitario":0.0,"precio_total":0.0}'

_SCHEMA = {
    "factura_compra": f'{{"tipo":"factura_compra","proveedor":"nombre o null","fecha":"DD/MM/YYYY o null","folio":"número o null","productos":[{_PROD}],"total":0.0}}',
    "nota_venta":     f'{{"tipo":"nota_venta","cliente":"nombre o null","fecha":"DD/MM/YYYY o null","folio":"número o null","productos":[{_PROD}],"total":0.0}}',
    "venta_publico":  f'{{"tipo":"venta_publico","fecha":"DD/MM/YYYY o null","folio":null,"productos":[{_PROD}],"total":0.0}}',
}

_MIME = {
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
    "ogg": "audio/ogg", "webm": "audio/webm", "mp4": "audio/mp4",
}

_LABEL = {
    "factura_compra": "factura de compra",
    "nota_venta":     "nota de venta",
    "venta_publico":  "venta al público (sin nota formal)",
}


def _strip_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return content.strip()


def extract_from_image(image_file, document_type: str) -> dict:
    raw = image_file.read()
    img = Image.open(io.BytesIO(raw))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()

    label = _LABEL[document_type]
    resp = _get_client().chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Eres experto en leer {label}s mexicanas (impreso o manuscrito). Extrae todos los campos incluyendo unidad de medida de cada producto. Responde SOLO JSON válido sin explicaciones:\n{_SCHEMA[document_type]}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        max_tokens=1400,
        temperature=0,
    )
    return json.loads(_strip_json(resp.choices[0].message.content))


def extract_from_text(text: str, document_type: str) -> dict:
    label = _LABEL[document_type]
    resp = _get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": f'Extrae datos de esta descripción de {label} mexicana. Infiere la unidad de medida si no se menciona explícitamente. El texto puede ser coloquial o tener errores:\n"{text}"\n\nResponde SOLO JSON válido:\n{_SCHEMA[document_type]}'}],
        max_tokens=1000,
        temperature=0,
    )
    return json.loads(_strip_json(resp.choices[0].message.content))


def extract_from_audio(audio_bytes: bytes, filename: str, document_type: str) -> tuple:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3"
    mime = _MIME.get(ext, "audio/mpeg")

    result = _get_client().audio.transcriptions.create(
        file=(filename, audio_bytes, mime),
        model=AUDIO_MODEL,
        language="es",
        response_format="text",
    )
    transcript = str(result)
    data = extract_from_text(transcript, document_type)
    return transcript, data


def extract_from_excel(file_bytes: bytes, filename: str, document_type: str) -> list:
    """Extrae registros de Excel/CSV. Si document_type='auto', detecta tipo por fila."""
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes))
    else:
        df = pd.read_excel(io.BytesIO(file_bytes))

    records = df.fillna("").to_dict("records")
    data_str = json.dumps(records, ensure_ascii=False, indent=2)

    if document_type == "auto":
        prompt = f"""Eres experto en interpretar hojas de cálculo de PyMEs mexicanas. Esta hoja PUEDE tener mezcla de compras y ventas.

Para cada registro determina si es compra (factura_compra) o venta (nota_venta / venta_publico) según el contexto (columnas como "Tipo", "proveedor", "cliente", palabras clave, etc.).

Datos:
{data_str}

Responde SOLO un array JSON. Para compras usa: {_SCHEMA['factura_compra']}
Para ventas con cliente usa: {_SCHEMA['nota_venta']}
Para ventas sin cliente usa: {_SCHEMA['venta_publico']}

Devuelve TODOS los registros en un array: [...]"""
    else:
        label = _LABEL[document_type]
        prompt = f"""Eres experto en interpretar hojas de cálculo de {label}s de PyMEs mexicanas. Incluye unidad de medida de cada producto.

Datos:
{data_str}

Responde SOLO un array JSON: [{_SCHEMA[document_type]}, ...]"""

    resp = _get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000,
        temperature=0,
    )
    return json.loads(_strip_json(resp.choices[0].message.content))


def analyze_business(context: str, period_label: str = "histórico completo") -> str:
    resp = _get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": f"""Eres consultor de PyMEs mexicanas especializado en "consultoría inversa": analizas datos de inventario, compras y ventas, y presentas lo más importante sin que el dueño te pida nada.

Período analizado: {period_label}

Datos del negocio:
{context}

Entrega el análisis con estas secciones (si no hay suficientes datos para alguna, dilo brevemente):

## 📊 Resumen Financiero
(totales, margen si hay ventas y compras, flujo estimado)

## 🔴 Alertas
(máximo 3, solo lo realmente crítico para este negocio)

## 💡 Top 5 Recomendaciones
(concretas para ESTE negocio, no genéricas — basadas en los datos reales)

## 📈 Oportunidades Detectadas
(qué podría hacer el negocio para crecer o reducir costos basado en sus datos)

Usa lenguaje simple y directo. Sin jerga técnica. El dueño es práctico."""}],
        max_tokens=2000,
        temperature=0.3,
    )
    return resp.choices[0].message.content


def chat_with_agent(context: str, history: list, question: str) -> str:
    messages = [
        {"role": "system", "content": f"Eres asesor práctico de PyMEs mexicanas experto en inventarios, compras y ventas. Datos del negocio:\n{context}\n\nSé directo y breve (máx 150 palabras). Si algo no está en los datos, dilo claramente."},
    ]
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    resp = _get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0.4,
    )
    return resp.choices[0].message.content
