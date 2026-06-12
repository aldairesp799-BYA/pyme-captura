# PyME Captura — Contexto del proyecto

## Qué es
App para que dueños de PyMEs mexicanas capturen facturas, notas de venta y
ventas al público sin esfuerzo. Foto → IA extrae datos → guardado en BD.
Canal adicional: bot de WhatsApp via Twilio.

## Brújula del proyecto
- **Norte:** Herramienta híbrida — útil para el emprendedor Y demostrable a un inversionista
- **Este:** Dueños de PyMEs en México atrapados en modo supervivencia
- **Sur:** El costo mental de registrar datos manualmente es el bloqueador principal
- **Oeste:** Tecnología accesible sin complejidad — si el flujo es complicado, no lo usan

## Decisiones de diseño
- **Data-capture-first**, no AI-agent-first: captura sin fricción primero, análisis después
- Flujo: Foto → auto-detecta tipo → revisión rápida → guardar (mínimo pasos)
- WhatsApp como canal secundario (canal más natural para PyMEs en México)

## Stack técnico
| Capa | Tecnología |
|---|---|
| Frontend | Streamlit (Python) |
| IA visión | Groq — `meta-llama/llama-4-scout-17b-16e-instruct` |
| IA texto/audio | Groq — `llama-3.3-70b-versatile` + Whisper |
| BD | SQLite local (Railway persiste en volumen) |
| WhatsApp webhook | FastAPI en Railway + Twilio |

## URLs de producción
- **App Streamlit:** https://pyme-captura-buz7bteaacz488mggyxm6j.streamlit.app
- **Webhook Railway:** https://web-production-22b02.up.railway.app

## Variables de entorno requeridas
- `GROQ_API_KEY` — en Streamlit secrets y Railway
- `TWILIO_ACCOUNT_SID` — en Railway
- `TWILIO_AUTH_TOKEN` — en Railway

## Para correr localmente
```
cd C:\Users\bryan\Projects\pyme-captura
python -m streamlit run app.py
```
Si hay ImportError al arrancar: borrar `__pycache__/` y reintentar.

## Estado actual (2026-06-10)
### Funciona
- Cámara: botón que activa la cámara (no pide permiso al cargar la página)
- Captura por galería, audio (grabación directa en navegador + subir archivo), texto libre, Excel/CSV
- Auto-detección de tipo de documento (compra / venta / venta pública)
- Quick review con TODOS los campos editables: tipo, fecha, proveedor/cliente, folio, zona, total, productos
- Referencia automática MMYY-NNN para venta_publico (ej: 0626-001)
- Checkbox "pendiente por confirmar" para nota_venta sin folio
- Alertas en quick review: sin fecha, sin proveedor, sin folio
- KPI de efectividad de captura (% sin corrección manual, desglose por medio)
- Dashboard: top proveedores + top clientes, top productos comprados + vendidos
- Columna Alertas en tab Registros (sin folio / sin fecha / pendiente)
- Agente IA con chat + diagnóstico de negocio
- WhatsApp webhook: taguea medio (whatsapp_imagen/audio/texto), marca sin_modificacion=True
- Extractor: prompts explícitos de usar fecha del DOCUMENTO, no la de hoy

### Bugs resueltos
- Error 401 Twilio media (commit aa90d62): _download no reenvía auth al CDN
- ImportError TIPO_LABEL: era __pycache__ desincronizado, no error de código

### Pendiente verificar
- Confirmar fix 401 con prueba real de audio/imagen por WhatsApp en Railway

## Schema de BD (documentos)
| Columna | Tipo | Notas |
|---|---|---|
| id | INTEGER PK | autoincrement |
| tipo | TEXT | factura_compra / nota_venta / venta_publico |
| fecha_captura | TEXT | DD/MM/YYYY HH:MM |
| fecha_documento | TEXT | DD/MM/YYYY (fecha real del doc) |
| entidad | TEXT | proveedor o cliente |
| folio | TEXT | número de folio o ref MMYY-NNN |
| total | REAL | |
| productos | TEXT | JSON array |
| zona | TEXT | zona/colonia del cliente |
| pendiente | INTEGER | 0/1 |
| sin_modificacion | INTEGER | 0/1 — KPI efectividad |
| medio | TEXT | canal de entrada |

## Próximo paso acordado
Capa de normalización de productos post-extracción:
- Problema: la IA puede extraer "anillo" y "estribo" como productos distintos
  cuando en el negocio son el mismo (variación por medida)
- Solución: catálogo de productos conocidos (desde historial SQLite) + LLM
  para normalizar nombres antes de guardar, en app Streamlit y webhook WhatsApp

## Archivos clave
- `app.py` — UI Streamlit completa
- `extractor.py` — lógica de extracción IA (imagen, audio, texto, Excel)
- `database.py` — SQLite: CRUD, KPI, referencias, exportar CSV
- `webhook.py` — FastAPI: webhook WhatsApp + descarga media Twilio
