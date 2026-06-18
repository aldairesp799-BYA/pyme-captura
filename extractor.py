import base64
import io
import json
import os
import re

from groq import Groq, RateLimitError
from PIL import Image
import pandas as pd

_client = None

def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def _llm(**kwargs):
    """Wrapper centralizado para chat completions — convierte RateLimitError en mensaje legible."""
    try:
        return _get_client().chat.completions.create(**kwargs)
    except RateLimitError as e:
        m = re.search(r"try again in ([^\.']+)", str(e))
        wait = m.group(1).strip() if m else "unos minutos"
        raise RuntimeError(f"Límite de tokens de Groq alcanzado. Intenta de nuevo en {wait}.") from e

TEXT_MODEL   = "llama-3.3-70b-versatile"
FAST_MODEL   = "llama-3.1-8b-instant"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
AUDIO_MODEL  = "whisper-large-v3"

_PROD = '{"nombre":"...","cantidad":0,"unidad":"kg/pz/lt/caja/etc","precio_unitario":0.0,"precio_total":0.0,"cantidad_fisica":null,"unidad_fisica":null}'

_UNIT_RULES = """REGLAS DE UNIDADES (obligatorio):
1. Toneladas → kg: si la cantidad está en toneladas, multiplica ×1000 y usa unidad='kg'.
2. Doble unidad (cuando se menciona TANTO cantidad física como peso):
   - cantidad = los KILOGRAMOS (es lo que determina el precio)
   - unidad = 'kg'
   - cantidad_fisica = número de bobinas / atados / rollos / piezas físicas
   - unidad_fisica = 'bobina' / 'atado' / 'rollo' / 'pz'
   - precio_total = cantidad(kg) × precio_unitario  ← NUNCA con cantidad_fisica
3. Productos solo-pieza (varilla, armex, tabique, cemento bolsa, clavo): unidad='pz' o 'saco', cantidad_fisica=null.
4. precio_total = cantidad × precio_unitario SIEMPRE."""

_SCHEMA = {
    "factura_compra": f'{{"tipo":"factura_compra","proveedor":"nombre o null","fecha":"DD/MM/YYYY real del documento (puede ser de meses/años anteriores — usa la fecha del documento, NO la de hoy) o null","folio":"número o null","productos":[{_PROD}],"total":0.0}}',
    "nota_venta":     f'{{"tipo":"nota_venta","cliente":"nombre o null","zona":"colonia/domicilio/zona del cliente si aparece, sino null","fecha":"DD/MM/YYYY real del documento (puede ser de meses/años anteriores — usa la fecha del documento, NO la de hoy) o null","folio":"número o null","productos":[{_PROD}],"total":0.0}}',
    "venta_publico":  f'{{"tipo":"venta_publico","zona":"colonia/domicilio/zona del cliente si aparece, sino null","fecha":"DD/MM/YYYY real del documento (puede ser de meses/años anteriores — usa la fecha del documento, NO la de hoy) o null","folio":null,"productos":[{_PROD}],"total":0.0}}',
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

_AUTO_SCHEMA = f"""{{
  "tipo": "factura_compra | nota_venta | venta_publico",
  "proveedor": "si es compra, sino null",
  "cliente": "si es venta con cliente identificable, sino null",
  "zona": "colonia, domicilio o zona del cliente si aparece en el documento, sino null",
  "fecha": "DD/MM/YYYY EXACTAMENTE como aparece en el documento — puede ser de semanas, meses o años anteriores, usa la fecha REAL del documento, NO la de hoy. null si no aparece.",
  "folio": "número de folio o null",
  "productos": [{_PROD}],
  "total": 0.0
}}"""


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
    resp = _llm(
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
    resp = _llm(
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

    resp = _llm(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000,
        temperature=0,
    )
    return json.loads(_strip_json(resp.choices[0].message.content))


def extract_from_image_auto(image_bytes: bytes) -> dict:
    """Extrae de imagen auto-detectando si es compra, venta o venta al público."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()

    resp = _llm(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Eres experto en documentos de PyMEs mexicanas. Determina si este documento es una compra (factura_compra), venta con cliente (nota_venta) o venta sin cliente (venta_publico). Extrae TODOS los campos incluyendo zona/domicilio del cliente.\n\n{_UNIT_RULES}\n\nResponde SOLO JSON válido:\n{_AUTO_SCHEMA}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        max_tokens=1400,
        temperature=0,
    )
    return json.loads(_strip_json(resp.choices[0].message.content))


_EMPTY_DOC = {
    "tipo": "factura_compra", "proveedor": None, "cliente": None,
    "zona": None, "fecha": None, "folio": None, "productos": [], "total": 0.0,
}

_CAT_SCHEMAS = {
    "cliente":    '{"nombre": "nombre canónico del cliente", "alias": ["como también se le conoce, puede estar vacío"], "zona": "zona, colonia o domicilio, null si no se menciona"}',
    "proveedor":  '{"nombre": "nombre canónico del proveedor", "alias": ["variantes del nombre, puede estar vacío"], "notas": "información relevante o cadena vacía"}',
    "producto":   '{"nombre": "nombre canónico del producto", "variantes": ["otras formas de llamarlo, ej: varilla 3/8, varilla tres octavos"], "unidad": "unidad principal: kg/pz/atado/rollo/etc", "notas": "info adicional o cadena vacía"}',
}

_CAT_EMPTY = {
    "cliente":   {"nombre": "", "alias": [], "zona": ""},
    "proveedor": {"nombre": "", "alias": [], "notas": ""},
    "producto":  {"nombre": "", "variantes": [], "unidad": "", "notas": ""},
}


def extract_catalogo_entry(text: str, tipo: str) -> dict:
    """Extrae datos de una entidad de catálogo (cliente/proveedor/producto) desde texto libre o dictado."""
    schema = _CAT_SCHEMAS.get(tipo, _CAT_SCHEMAS["cliente"])
    prompt = (
        f'Extrae la información de este {tipo} desde el texto. '
        f'El texto puede ser coloquial o dictado por voz en español mexicano.\n\n'
        f'Texto: "{text}"\n\nResponde SOLO JSON válido con este schema:\n{schema}'
    )
    resp = _llm(
        model=FAST_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0,
    )
    raw = _strip_json(resp.choices[0].message.content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        fix = _llm(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": f'Corrige este JSON malformado y devuelve SOLO el JSON corregido:\n\n{raw}'}],
            max_tokens=400,
            temperature=0,
        )
        return json.loads(_strip_json(fix.choices[0].message.content))
    except Exception:
        return dict(_CAT_EMPTY.get(tipo, {}))


def extract_from_text_auto(text: str) -> dict:
    """Extrae de texto libre auto-detectando tipo de documento."""
    resp = _llm(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": f'Eres experto en operaciones de PyMEs mexicanas. Determina si es compra o venta. El texto puede ser coloquial o dictado por voz.\n\n{_UNIT_RULES}\n\nTexto:\n"{text}"\n\nResponde SOLO JSON válido:\n{_AUTO_SCHEMA}'}],
        max_tokens=1200,
        temperature=0,
    )
    raw = _strip_json(resp.choices[0].message.content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        fix_resp = _llm(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": f'El siguiente texto debe ser JSON válido pero está malformado. Corrígelo y devuelve SOLO el JSON corregido, sin explicaciones:\n\n{raw}'}],
            max_tokens=1200,
            temperature=0,
        )
        return json.loads(_strip_json(fix_resp.choices[0].message.content))
    except Exception:
        return dict(_EMPTY_DOC)


def extract_from_audio_auto(audio_bytes: bytes, filename: str) -> tuple:
    """Transcribe audio y extrae datos auto-detectando tipo."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    mime = _MIME.get(ext, "audio/ogg")
    result = _get_client().audio.transcriptions.create(
        file=(filename, audio_bytes, mime),
        model=AUDIO_MODEL,
        language="es",
        response_format="text",
    )
    transcript = str(result)
    data = extract_from_text_auto(transcript)
    return transcript, data


def normalize_product_names(products: list, known_products: list) -> list:
    """Normaliza nombres extraídos contra el catálogo histórico del negocio."""
    if len(known_products) < 3 or not products:
        return products
    extracted = [p.get("nombre", "") for p in products if p.get("nombre")]
    if not extracted:
        return products
    catalog = "\n".join(f"- {p}" for p in known_products[:60])
    resp = _llm(
        model=FAST_MODEL,
        messages=[{"role": "user", "content": f"""Eres experto en ferretería y materiales de construcción mexicanos.

Catálogo de productos del negocio:
{catalog}

Nombres recién extraídos (pueden ser variantes, abreviaciones o sinónimos):
{json.dumps(extracted, ensure_ascii=False)}

Para cada nombre extraído: si hay coincidencia clara con el catálogo (mismo producto escrito diferente), usa el nombre del catálogo. Si no hay coincidencia, deja el original.

Responde SOLO JSON array en el mismo orden que los nombres extraídos:
["nombre_1", "nombre_2", ...]"""}],
        max_tokens=300,
        temperature=0,
    )
    try:
        normalized = json.loads(_strip_json(resp.choices[0].message.content))
        j = 0
        for p in products:
            if p.get("nombre"):
                if j < len(normalized) and normalized[j]:
                    p["nombre"] = normalized[j]
                j += 1
    except Exception:
        pass
    return products


def analyze_business(context: str, period_label: str = "histórico completo") -> str:
    resp = _llm(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": f"""Eres consultor operativo de una PyME mexicana (ferretería/materiales de construcción).

USA siempre la fecha del DOCUMENTO para análisis temporal, nunca la fecha de captura.

Período: {period_label}

Datos:
{context}

## 📊 Resumen del período
(ventas, compras, margen global)

## 📦 Productos y movimientos
- Cuáles se mueven más (volumen y dinero)
- Productos vendidos sin compra registrada (riesgo de abasto)
- Margen por producto donde haya datos de compra y venta
- Precio de compra vs precio de venta

## 👥 Clientes
- Más frecuentes / mayor valor
- Zonas con más actividad

## 🔴 Alertas (máximo 3)

## 💡 Recomendaciones (máximo 5, basadas en datos reales)

Lenguaje directo. Omite secciones sin datos."""}],
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

    resp = _llm(
        model=TEXT_MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0.4,
    )
    return resp.choices[0].message.content
