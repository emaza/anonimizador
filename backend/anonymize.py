"""Anonimización: regex (deterministas) + Ollama (NER) con deduplicación estable.

Flujo:
  1) Regex sobre patrones fijos: email, DNI/NIE, CIF, IBAN/cuenta, importe, teléfono.
  2) El texto ya con placeholders se manda a Ollama, que devuelve SOLO JSON con
     personas / empresas / direcciones.
  3) Se valida el JSON (1 reintento). Si falla, se lanza AnonymizeError.
  4) Misma entidad -> mismo placeholder (dedup por valor normalizado).

Nada se escribe en disco ni se registra el contenido.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

OLLAMA_URL = "http://localhost:11434"


class AnonymizeError(Exception):
    """Error controlado, con mensaje apto para mostrar al usuario."""


# --- Reglas regex, en orden de aplicación (las más específicas primero) ---------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# DNI: 8 dígitos + letra. NIE: X/Y/Z + 7 dígitos + letra. Admite puntos de miles
# (45.812.339-K) y guion opcional antes de la letra.
DNI_RE = re.compile(
    r"(?<![\dA-Za-z])"
    r"(?:[XYZxyz]-?\d(?:\.?\d){6}|\d(?:\.?\d){7})"
    r"-?[A-Za-z]"
    r"(?![\dA-Za-z])"
)

# CIF/NIF de empresa: letra de entidad + 7 dígitos + control (dígito o A-J). Admite
# prefijo ES y guion/puntos (B-83839647, B83839647, ESB83839647). X/Y/Z son NIE.
CIF_RE = re.compile(
    r"(?<![\dA-Za-z])"
    r"(?:ES[ \-]?)?"
    r"[ABCDEFGHJNPQRSUVWabcdefghjnpqrsuvw]-?"
    r"\d(?:\.?\d){6}"
    r"-?[0-9A-Ja-j]"
    r"(?![\dA-Za-z])"
)

# IBAN: país + control + al menos 2 grupos de 4 dígitos (capta también IBANs
# parciales/truncados como "ES91 2100 0418", no solo el completo). Y CCC de 20 dígitos.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ \-]?\d{4}){2,7}(?:[ \-]?\d{1,3})?\b")
CCC_RE = re.compile(r"(?<![\d])\d{4}[ \-]?\d{4}[ \-]?\d{2}[ \-]?\d{2}[ \-]?\d{10}(?![\d])")

# Importes: con símbolo/palabra de moneda, o formato con miles y decimales (1.500,00).
IMPORTE_RE = re.compile(
    r"(?:(?:€|EUR|eur)\s?\d[\d.,]*)"
    r"|(?:\d[\d.,]*\s?(?:€|EUR|eur|euros?))"
    r"|(?:\b\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?\b)"
)

# Teléfono España: 9 dígitos, prefijo +34/0034 opcional, separadores opcionales.
TELEFONO_RE = re.compile(
    r"(?<![\d\w])(?:(?:\+34|0034)[ \-]?)?"
    r"(?:\d{3}[ \-]?\d{3}[ \-]?\d{3}|\d{3}[ \-]?\d{2}[ \-]?\d{2}[ \-]?\d{2})"
    r"(?![\d\w])"
)

# Empresas con forma jurídica española (respaldo determinista del modelo).
# Captura hasta 4 palabras (Mayúscula o conector) antes de la forma jurídica.
_LEGAL = (
    r"S\.L\.U\.|S\.L\.L\.|S\.A\.U\.|S\.Coop\.|S\.C\.P\.|S\.R\.L\.|S\.A\.T\.|"
    r"S\.L\.|S\.A\.|S\.C\."
)
_NOT_LEGAL = r"(?!(?:" + _LEGAL + r"))"
EMPRESA_RE = re.compile(
    _NOT_LEGAL + r"[A-ZÁÉÍÓÚÑ][\w'&.\-]*"
    r"(?:\s+(?:de|del|la|las|los|y|e|" + _NOT_LEGAL + r"[A-ZÁÉÍÓÚÑ][\w'&.\-]*)){0,4}"
    r"\s+(?:" + _LEGAL + r")"
)

# Domicilios/direcciones con tipo de vía + nombre + número (respaldo determinista
# del modelo). Captura tanto direcciones completas ("Paseo de Gracia 92, 08008
# Barcelona") como parciales sin código postal ("Calle Goya 15", "C/ Alcalá 200").
_VIA = (
    r"(?:C/|Calle|Avda\.?|Avenida|Av\.|Plaza|Pza\.?|Pl\.|Paseo|Ronda|Carretera|"
    r"Ctra\.?|Camino|Travesía|Glorieta|Callejón|Vía|Urbanización|Urb\.?)"
)
# Nombre de la vía: conectores iniciales opcionales + al menos una palabra en mayúscula.
_VIA_NOMBRE = (
    r"(?:(?:de|del|la|las|los|y)\s+){0,2}"
    r"[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ'’\-]*"
    r"(?:\s+(?:de|del|la|las|los|y|[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ'’\-]*)){0,4}"
)
# Detalles tras el número (piso, puerta, CP, ciudad): deben parecer de dirección
# (empiezan por dígito, mayúscula o palabra de piso) para no tragar frases sueltas.
_VIA_DETALLE = (
    r"(?:\s*,\s*(?:\d[^,\n.]{0,30}|[A-ZÁÉÍÓÚÑ][^,\n.]{0,30}|"
    r"(?:bajo|ático|entresuelo|principal|izquierda|derecha|izq|dcha|planta|puerta|"
    r"piso|esc|pta)[^,\n.]{0,30})){0,3}"
)
DIRECCION_RE = re.compile(
    _VIA + r"\s+" + _VIA_NOMBRE +
    r",?\s+(?:n[.ºo]?\s*)?\d{1,4}(?:\s?bis)?[A-Za-zºª]?" + _VIA_DETALLE
)

# Patrones fijos: se aplican ANTES del modelo (el modelo ve ya los marcadores).
REGEX_RULES_FIJOS = [
    ("EMAIL", "EMAIL", EMAIL_RE),
    ("DNI", "DNI", DNI_RE),
    ("CIF", "CIF", CIF_RE),
    ("CUENTA", "CUENTA", IBAN_RE),
    ("CUENTA", "CUENTA", CCC_RE),
    ("IMPORTE", "IMPORTE", IMPORTE_RE),
    ("TELEFONO", "TELEFONO", TELEFONO_RE),
]

# Respaldo determinista: se aplica DESPUÉS del modelo, para que el modelo fije
# primero los límites de personas/empresas y el regex no invada un nombre propio
# (p. ej. "Marta Sánchez de Cárnicas del Ebro S.A."). Al arrancar solo en mayúscula,
# el regex empieza tras el marcador [PERSONA_x] y no cruza el conector "de".
REGEX_RULES_RESPALDO = [
    ("EMPRESA", "EMPRESA", EMPRESA_RE),
    ("DIRECCION", "DIRECCION", DIRECCION_RE),
]


def _norm(category: str, value: str) -> str:
    """Clave de deduplicación: misma entidad -> mismo placeholder."""
    v = value.strip()
    if category == "EMAIL":
        return v.lower()
    if category in ("TELEFONO", "DNI", "CIF", "CUENTA"):
        return re.sub(r"[\s\-]", "", v).upper()
    if category == "IMPORTE":
        return re.sub(r"\s", "", v)
    # PERSONA / EMPRESA / DIRECCION
    return re.sub(r"\s+", " ", v).casefold()


@dataclass
class Anonymizer:
    counters: dict[str, int] = field(default_factory=dict)
    norm_to_placeholder: dict[str, str] = field(default_factory=dict)
    # Preserva orden de aparición para la tabla del frontend.
    mapping: dict[str, dict] = field(default_factory=dict)  # placeholder -> {original, category}

    def placeholder_for(self, category: str, prefix: str, original: str) -> str:
        key = f"{category}::{_norm(category, original)}"
        existing = self.norm_to_placeholder.get(key)
        if existing:
            return existing
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        placeholder = f"[{prefix}_{self.counters[prefix]}]"
        self.norm_to_placeholder[key] = placeholder
        self.mapping[placeholder] = {"original": original.strip(), "category": category}
        return placeholder

    def apply_regex(self, text: str, rules) -> str:
        for category, prefix, regex in rules:
            def repl(m: re.Match, _cat=category, _pre=prefix) -> str:
                return self.placeholder_for(_cat, _pre, m.group(0))

            text = regex.sub(repl, text)
        return text

    def apply_entities(self, text: str, category: str, prefix: str, values: list[str]) -> str:
        """Sustituye por longitud descendente para no romper entidades solapadas."""
        seen: set[str] = set()
        clean: list[str] = []
        for v in values:
            if not isinstance(v, str):
                continue
            v = v.strip()
            # Descarta vacíos, ruido de 1 char y algo que ya sea un placeholder.
            if len(v) < 2 or v.startswith("["):
                continue
            if v not in seen:
                seen.add(v)
                clean.append(v)

        for v in sorted(clean, key=len, reverse=True):
            if v not in text:
                continue
            placeholder = self.placeholder_for(category, prefix, v)
            text = text.replace(v, placeholder)
        return text

    def mapping_list(self) -> list[dict]:
        return [
            {"placeholder": ph, "original": info["original"], "category": info["category"]}
            for ph, info in self.mapping.items()
        ]


# --- Ollama ---------------------------------------------------------------------

_PROMPT = """Eres un extractor de entidades para anonimizar texto en español.

Analiza el TEXTO y extrae SOLO estas categorías, copiando las cadenas EXACTAMENTE como aparecen:
- "personas": nombres de personas (nombre y/o apellidos).
- "empresas": nombres de empresas, marcas u organizaciones. Incluye SIEMPRE las que llevan
  forma jurídica como S.L., S.A., S.L.U., S.A.U., S.Coop., S.C. o similares, y toma el nombre
  completo con esa forma jurídica.
- "direcciones": domicilios y direcciones postales, COMPLETAS o PARCIALES (incluye
  calle y número aunque falten el piso, el código postal o la ciudad).

Reglas:
- NO incluyas emails, teléfonos, DNI, CIF, importes ni cuentas: ya están sustituidos por marcadores como [EMAIL_1].
- NO inventes datos que no estén en el texto.
- Copia cada valor tal cual aparece, sin reformatear.
- Responde ÚNICAMENTE con un objeto JSON válido, sin explicaciones ni texto adicional.

EJEMPLO (datos ficticios)
Texto: "Ana Ejemplo, de Talleres Ficticios S.L., vive en Calle Inventada 1, 00000 Villaejemplo."
Respuesta: {"personas": ["Ana Ejemplo"], "empresas": ["Talleres Ficticios S.L."], "direcciones": ["Calle Inventada 1, 00000 Villaejemplo"]}

Formato de salida EXACTO:
{"personas": [], "empresas": [], "direcciones": []}

TEXTO:
<<<
%s
>>>
"""


def _extract_json(raw: str) -> dict:
    """Extrae el primer objeto JSON válido, tolerando <think>...</think> u otro ruido."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Busca el primer { ... } balanceado.
    start = raw.find("{")
    if start == -1:
        raise ValueError("sin JSON en la respuesta")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start : i + 1])
    raise ValueError("JSON incompleto en la respuesta")


def _validate(obj: dict) -> dict[str, list[str]]:
    if not isinstance(obj, dict):
        raise ValueError("la respuesta no es un objeto")
    result: dict[str, list[str]] = {}
    for key in ("personas", "empresas", "direcciones"):
        value = obj.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValueError(f"'{key}' no es una lista")
        result[key] = [str(x) for x in value]
    return result


def call_ollama(text: str, model: str, timeout: float = 180.0) -> dict[str, list[str]]:
    """Llama a Ollama pidiendo JSON. Reintenta una vez si el JSON viene mal."""
    payload = {
        "model": model,
        "prompt": _PROMPT % text,
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0},
    }

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        except httpx.ConnectError as exc:
            raise AnonymizeError(
                "No se pudo conectar con Ollama en localhost:11434. "
                "Arranca Ollama y asegúrate de haber descargado el modelo."
            ) from exc
        except httpx.HTTPError as exc:
            last_err = exc
            continue

        if resp.status_code == 404:
            raise AnonymizeError(
                f"El modelo '{model}' no está disponible en Ollama. "
                f"Descárgalo con:  ollama pull {model}"
            )
        if resp.status_code >= 400:
            last_err = AnonymizeError(f"Ollama devolvió un error {resp.status_code}.")
            # 'think' puede no estar soportado en versiones viejas: reintenta sin él.
            payload.pop("think", None)
            continue

        raw = resp.json().get("response", "")
        try:
            return _validate(_extract_json(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            continue

    raise AnonymizeError(
        "El modelo no devolvió un JSON válido tras un reintento. "
        "No se ha anonimizado nada para no mostrarte un texto a medias. "
        "Prueba de nuevo o revisa que el modelo esté bien descargado."
    ) from last_err


# --- Orquestación ---------------------------------------------------------------

# Categorías que detecta el modelo (el resto son deterministas por regex).
CATEGORIAS_MODELO = {"PERSONA", "EMPRESA", "DIRECCION"}
# Todas las categorías admitidas.
CATEGORIAS = ["PERSONA", "EMPRESA", "EMAIL", "TELEFONO", "DNI", "CIF", "IMPORTE", "DIRECCION", "CUENTA"]


def anonymize(text: str, model: str, categorias=None) -> dict:
    """Anonimiza `text` limitándose a las `categorias` activas.

    `categorias`: iterable con nombres de CATEGORIAS. Si es None, se anonimizan todas.
    Las categorías no incluidas se dejan intactas en el texto.
    Devuelve {'anonymized': str, 'mapping': [ {placeholder, original, category} ]}.
    """
    if not text or not text.strip():
        raise AnonymizeError("No hay texto para anonimizar.")

    if categorias is None:
        activas = set(CATEGORIAS)
    else:
        activas = {c for c in categorias if c in CATEGORIAS}
    if not activas:
        raise AnonymizeError("No has seleccionado ninguna categoría que anonimizar.")

    engine = Anonymizer()

    # 1) Patrones fijos activos (email, DNI, CIF, cuenta, importe, teléfono).
    text = engine.apply_regex(text, [r for r in REGEX_RULES_FIJOS if r[0] in activas])

    # 2) Modelo: solo si alguna categoría suya está activa (si no, ni se llama a Ollama).
    if activas & CATEGORIAS_MODELO:
        entities = call_ollama(text, model)
        if "PERSONA" in activas:
            text = engine.apply_entities(text, "PERSONA", "PERSONA", entities["personas"])
        if "EMPRESA" in activas:
            text = engine.apply_entities(text, "EMPRESA", "EMPRESA", entities["empresas"])
        if "DIRECCION" in activas:
            text = engine.apply_entities(text, "DIRECCION", "DIRECCION", entities["direcciones"])

    # 3) Respaldo determinista activo, para lo que el modelo haya dejado escapar.
    text = engine.apply_regex(text, [r for r in REGEX_RULES_RESPALDO if r[0] in activas])

    return {"anonymized": text, "mapping": engine.mapping_list()}
