# Anonimizador · beailabs — Diseño

Fecha: 2026-07-18

## Objetivo
App web local (localhost) que anonimiza texto sensible usando Ollama como motor de IA,
100% en la máquina del usuario: cero APIs externas, cero CDNs, todo el código y librerías en local.
Permite luego des-anonimizar la respuesta que devuelva un LLM externo (ej. ChatGPT).

## Decisiones cerradas
- **Modelo Ollama**: `qwen3:4b` (endpoint `http://localhost:11434`).
- **Arquitectura**: backend Python (FastAPI + uvicorn) que sirve el frontend y hace de proxy a Ollama.
- **Ollama**: lo instala el usuario aparte; la app detecta si está arriba y avisa si no.
- **Deps Python**: pip desde PyPI en instalación inicial es aceptable. En uso, cero red externa.

## Arquitectura
Backend FastAPI en `http://localhost:8000`:
- Sirve frontend estático (HTML/CSS/JS vanilla, sin frameworks, sin CDN; todo vendorizado local).
- `POST /api/extract`: recibe archivo (.txt/.md/.docx/.pdf), extrae texto en local
  (python-docx para docx, pdfplumber para pdf). PDF sin capa de texto → aviso "sin texto extraíble".
  Nada se guarda en disco; el archivo se procesa en memoria y se descarta.
- `POST /api/anonymize`: aplica regex + llama a Ollama para NER. Devuelve texto anonimizado + mapa.
- `GET /api/health`: comprueba si Ollama responde y si el modelo está disponible.
- Proxy a Ollama desde el backend → sin CORS ni llamadas externas desde el navegador.
- Sin persistencia ni logging del contenido. Texto original solo en memoria de la request
  y en el JS del navegador; muere al cerrar la pestaña.

## Flujo de anonimización (fiabilidad)
1. **Regex de patrones fijos** primero (deterministas, más fiables que el modelo):
   Email, teléfono (España), DNI/NIE, IBAN / número de cuenta, importes (€, EUR, 1.500,00).
2. Texto (con placeholders regex ya aplicados) → **Ollama** con prompt que exige SOLO JSON:
   `{ "personas": [], "empresas": [], "direcciones": [] }`.
3. **Validación** del JSON. Si es inválido → **1 reintento**. Si falla otra vez → error claro
   en pantalla; nunca se muestra texto a medio anonimizar.
4. **Respaldo regex determinista** (tras el modelo): empresas con forma jurídica
   (S.L., S.A., S.L.U., S.Coop.…) y direcciones con tipo de vía + código postal que el modelo
   haya omitido. Se ejecuta DESPUÉS del modelo a propósito: así el modelo fija primero los
   límites de personas/empresas y el regex, que solo arranca en mayúscula, no invade un nombre
   propio (p. ej. no parte "Nombre Apellido de Empresa Ficticia S.L.").
5. **Deduplicación**: misma entidad (texto normalizado) → mismo placeholder estable.
   Categorías: PERSONA, EMPRESA, EMAIL, TELEFONO, DNI, IMPORTE, DIRECCION, CUENTA.

## UI (3 paneles)
- **Entrada**: textarea grande + zona drop (.txt/.md/.docx/.pdf). Al soltar, muestra el texto
  extraído para revisar. Botón "Anonimizar" con indicador "procesando" + cronómetro.
- **Resultado**: texto anonimizado con placeholders resaltados en color; botón "Copiar".
- **Tabla plegable**: mapa placeholder → valor original.
- **Des-anonimizar**: se pega la respuesta del LLM externo; sustituye placeholders → valores
  originales usando el mapa en memoria. Botón "Copiar".

## Diseño visual
- Fondo `#FAFAFF`, tarjetas `#F4F6FF`, acento primario `#3D3D8F`, secundario `#7C5CFC`,
  texto principal `#1A1A2E`, texto secundario `#6B6B8A`, tarjetas flotantes blancas con sombra suave.
- Tipografía del sistema, limpia. Interfaz en español. Título "Anonimizador · beailabs".

## Estructura de archivos
```
ollama-app/
  backend/
    app.py            # FastAPI: rutas, sirve estáticos
    extract.py        # extracción txt/md/docx/pdf
    anonymize.py      # regex + orquestación Ollama + dedup
  static/
    index.html
    styles.css
    app.js
  requirements.txt
  run.sh              # venv + pip + uvicorn + abre navegador
  README.md           # instrucciones (incluye instalar Ollama + pull qwen3:4b)
```

## No-objetivos (YAGNI)
- Sin OCR de PDFs escaneados (solo aviso).
- Sin base de datos, sin autenticación, sin persistencia.
- Sin frameworks JS ni build step.
```
