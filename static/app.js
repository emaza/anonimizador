"use strict";

// Estado en memoria. Nunca se guarda en disco ni se envía fuera.
let currentMapping = [];               // [{placeholder, original, category}]
let anonymizedText = "";               // texto anonimizado en crudo (para copiar)

const $ = (id) => document.getElementById(id);

// Estado vacío del panel de resultado (candado + dos líneas).
const EMPTY_OUTPUT_HTML = `
  <div class="empty-state">
    <svg width="60" height="60" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M7 10.5V7.5a5 5 0 0 1 10 0v3" stroke="#C7B8FF" stroke-width="1.3" fill="none" stroke-linecap="round"/>
      <rect x="4.75" y="10.5" width="14.5" height="10" rx="2.6" stroke="#C7B8FF" stroke-width="1.3" fill="none"/>
      <circle cx="12" cy="14.7" r="1.4" fill="#C7B8FF"/>
      <path d="M12 15.8v2.1" stroke="#C7B8FF" stroke-width="1.3" stroke-linecap="round"/>
    </svg>
    <p class="empty-title">Aquí verás tu texto limpio</p>
    <p class="empty-sub">Los datos sensibles se convertirán en etiquetas de color</p>
  </div>`;

// --- utilidades ----------------------------------------------------------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const PH_RE = /\[([A-Z]+)_(\d+)\]/g;

// Solo resalta los marcadores que existen en el mapa actual; así los corchetes que
// el propio texto original ya trajera ([PERSONA_1] escrito a mano) no se pintan como
// si se hubieran anonimizado.
function highlight(text) {
  const known = new Set(currentMapping.map((m) => m.placeholder));
  return escapeHtml(text).replace(PH_RE, (m, cat) =>
    known.has(m) ? `<span class="ph ph-${cat}">${m}</span>` : m
  );
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2200);
}

function showMsg(el, text, kind) {
  el.textContent = text;
  el.className = `msg msg--${kind}`;
  el.hidden = false;
}
function hideMsg(el) { el.hidden = true; }

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copiado al portapapeles");
  } catch {
    // Fallback sin API de portapapeles.
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
    toast("Copiado al portapapeles");
  }
}

// --- estado de Ollama ----------------------------------------------------
async function checkHealth() {
  const el = $("status");
  const txt = $("status-text");
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    if (!h.ollama_up) {
      el.className = "status status--down";
      txt.textContent = "Ollama no está arrancado";
    } else if (!h.model_ready) {
      el.className = "status status--warn";
      txt.textContent = `Falta el modelo ${h.model} (ollama pull ${h.model})`;
    } else {
      el.className = "status status--up";
      txt.textContent = `Ollama listo · ${h.model}`;
    }
  } catch {
    el.className = "status status--down";
    txt.textContent = "No se pudo comprobar Ollama";
  }
}

// --- extracción de archivos ---------------------------------------------
async function handleFile(file) {
  const msg = $("extract-msg");
  hideMsg(msg);
  const name = (file.name || "").toLowerCase();
  const ok = [".txt", ".md", ".docx", ".pdf"].some((e) => name.endsWith(e));
  if (!ok) {
    showMsg(msg, "Formato no soportado. Usa .txt, .md, .docx o .pdf.", "error");
    return;
  }

  showMsg(msg, `Extrayendo texto de “${file.name}”…`, "info");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/extract", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) {
      // 422 => aviso claro (p. ej. PDF escaneado).
      showMsg(msg, data.error || "No se pudo extraer el texto.", "warn");
      return;
    }
    $("input").value = data.text;
    showMsg(msg, `Texto extraído de “${file.name}”. Revísalo antes de anonimizar.`, "info");
  } catch {
    showMsg(msg, "Error al procesar el archivo.", "error");
  }
}

function setupDropzone() {
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragleave" && dz.contains(e.relatedTarget)) return;
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) handleFile(file);
  });
}

// --- anonimizar ----------------------------------------------------------
let timerHandle = null;
function startTimer() {
  const t0 = performance.now();
  $("timer").textContent = "0.0";
  $("progress").hidden = false;
  timerHandle = setInterval(() => {
    $("timer").textContent = ((performance.now() - t0) / 1000).toFixed(1);
  }, 100);
}
function stopTimer() {
  clearInterval(timerHandle);
  $("progress").hidden = true;
}

function renderMapping() {
  const rows = $("map-rows");
  rows.innerHTML = "";
  for (const item of currentMapping) {
    const tr = document.createElement("tr");
    const c1 = document.createElement("td");
    c1.innerHTML = `<span class="ph ph-${item.category}">${escapeHtml(item.placeholder)}</span>`;
    const c2 = document.createElement("td");
    c2.textContent = item.original;
    const c3 = document.createElement("td");
    c3.innerHTML = `<span class="tag">${item.category}</span>`;
    tr.append(c1, c2, c3);
    rows.appendChild(tr);
  }
  $("map-count").textContent = String(currentMapping.length);
}

async function anonymize() {
  const text = $("input").value.trim();
  const msg = $("anon-msg");
  hideMsg(msg);
  if (!text) {
    showMsg(msg, "Pega o extrae algún texto antes de anonimizar.", "warn");
    return;
  }

  const categories = Array.from(
    document.querySelectorAll("#cat-row input:checked")
  ).map((c) => c.value);
  if (categories.length === 0) {
    showMsg(msg, "Marca al menos una categoría que anonimizar.", "warn");
    return;
  }

  $("btn-anon").disabled = true;
  startTimer();
  try {
    const r = await fetch("/api/anonymize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, categories }),
    });
    const data = await r.json();
    if (!r.ok) {
      showMsg(msg, data.error || "No se pudo anonimizar.", "error");
      return;
    }
    anonymizedText = data.anonymized;
    currentMapping = data.mapping;
    $("output").innerHTML = highlight(anonymizedText) ||
      '<p class="placeholder-empty">El texto no contenía datos sensibles detectables.</p>';
    $("btn-copy-out").disabled = false;
    renderMapping();
    if (currentMapping.length === 0) {
      showMsg(msg, "No se detectaron datos sensibles en el texto.", "info");
    }
  } catch {
    showMsg(msg, "Error de conexión con el servidor local.", "error");
  } finally {
    stopTimer();
    $("btn-anon").disabled = false;
  }
}

// --- des-anonimizar ------------------------------------------------------
function deanonymize() {
  let text = $("deanon-input").value;
  if (!text.trim()) { toast("Pega primero la respuesta a des-anonimizar"); return; }
  if (currentMapping.length === 0) { toast("Aún no hay mapa: anonimiza un texto primero"); return; }

  // Reemplaza los marcadores más largos primero, por seguridad.
  const ordered = [...currentMapping].sort((a, b) => b.placeholder.length - a.placeholder.length);
  for (const item of ordered) {
    text = text.split(item.placeholder).join(item.original);
  }
  $("deanon-output").textContent = text;
  $("btn-copy-deanon").disabled = false;
}

// --- init ----------------------------------------------------------------
function init() {
  setupDropzone();
  checkHealth();
  setInterval(checkHealth, 15000);

  $("btn-anon").addEventListener("click", anonymize);
  $("btn-copy-out").addEventListener("click", () => copyText(anonymizedText));
  $("btn-deanon").addEventListener("click", deanonymize);
  $("btn-copy-deanon").addEventListener("click", () =>
    copyText($("deanon-output").textContent)
  );

  $("btn-clear").addEventListener("click", () => {
    $("input").value = "";
    $("output").innerHTML = EMPTY_OUTPUT_HTML;
    $("deanon-input").value = "";
    $("deanon-output").innerHTML = '<p class="placeholder-empty">Aquí verás el texto con los valores reales.</p>';
    currentMapping = []; anonymizedText = "";
    renderMapping();
    hideMsg($("extract-msg")); hideMsg($("anon-msg"));
    $("btn-copy-out").disabled = true; $("btn-copy-deanon").disabled = true;
  });

  const toggle = $("map-toggle");
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!open));
    $("map-body").hidden = open;
  });
}

document.addEventListener("DOMContentLoaded", init);
