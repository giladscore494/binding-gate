"""The offline labelling page: one static HTML file per audit sample.

Properties
----------
* **No server, no network.** One file with inline CSS and a small inline script;
  no external asset, font, library or request. It opens from disk (``file://``)
  in any current browser.
* **Blind first.** Each case shows the blinded view of
  :mod:`binding_gate.audit_tool.views`. The reveal data (verdict, reasons,
  checks, constraints, ids) are stored per case as base64-encoded JSON in a
  ``data-reveal`` attribute and decoded only after the auditor has chosen a
  label and pressed "Lock label and reveal". This keeps the verdict out of the
  visible page and out of a casual look at the source; it is not encryption,
  and the auditor is instructed not to decode it.
* **Locked labels.** Revealing a case locks its label; the export marks every
  label as entered before the reveal (``label_entered_before_reveal``), which the
  analysis requires.
* **Progress** is kept in the browser's ``localStorage`` under a key derived
  from the sample's SHA-256. Every storage access is wrapped in ``try``/``catch``;
  if storage is unavailable (for example some browsers' private mode for
  ``file://`` pages) the page says so and keeps working in memory.
* **Export** writes the label file (format in
  :mod:`binding_gate.audit_tool.labels`) as a JSON download; if the download is
  blocked, the JSON is shown in a text box to copy. **Import** reads a label file
  back, for example to continue in another browser.
"""

from __future__ import annotations

import base64
import html
import json
from typing import Any, Mapping

from binding_gate.audit_tool import prereg
from binding_gate.audit_tool.labels import LABELS_FORMAT, LABELS_VERSION
from binding_gate.audit_tool.views import blinded_view, reveal_payload

_LABEL_TEXT = {"yes": "Yes", "no": "No", "cannot_tell": "Cannot tell"}
_DECOMPOSITION_TEXT = {
    "not_assessed": "not assessed",
    "constraints_correct": "constraints are correct",
    "wrong_entity": "wrong entity",
    "wrong_or_missing_qualifier": "wrong or missing qualifier",
    "wrong_or_missing_presupposition": "wrong or missing presupposition",
    "other_decomposition_problem": "other decomposition problem",
}

_CSS = """
:root { --ink:#1d1d1b; --muted:#5f5e5a; --line:#d9d8d2; --bg:#fbfbf9; --card:#ffffff; --accent:#2a5fa8; --warn:#8a4b00; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); }
header { position: sticky; top: 0; background: var(--card); border-bottom: 1px solid var(--line); padding: 10px 20px; z-index: 2; }
header h1 { font-size: 17px; margin: 0 0 4px; }
header .bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
main { max-width: 980px; margin: 0 auto; padding: 16px 20px 80px; }
.case { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 16px 18px; margin: 18px 0; }
.case h2 { font-size: 16px; margin: 0 0 8px; }
.case.done { border-left: 5px solid var(--accent); }
.question { font-weight: 600; }
.doc { border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; margin: 8px 0; white-space: pre-wrap; }
.doc h3 { font-size: 13px; margin: 0 0 4px; color: var(--muted); }
.quote { background: #f3f1e8; border-left: 3px solid var(--muted); padding: 6px 10px; white-space: pre-wrap; }
.labelbox { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line); }
.labelbox label { margin-right: 14px; }
textarea { width: 100%; min-height: 44px; font: inherit; }
button { font: inherit; padding: 4px 12px; border-radius: 5px; border: 1px solid var(--line); background: #f2f2ee; cursor: pointer; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button:disabled { opacity: .5; cursor: default; }
.reveal { margin-top: 12px; background: #f6f8fc; border: 1px solid #c9d6ea; border-radius: 6px; padding: 10px 12px; }
.reveal dt { font-weight: 600; margin-top: 4px; }
.reveal dd { margin: 0 0 4px 0; }
.warn { color: var(--warn); }
.muted { color: var(--muted); font-size: 13px; }
#exportbox { width: 100%; min-height: 120px; display: none; }
"""

_SCRIPT = r"""
(function () {
  "use strict";
  var META = JSON.parse(document.getElementById("audit-meta").textContent);
  var KEY = "binding-gate-audit/" + META.sample_sha256;
  var state = { auditor: "", labels: {} };
  var storageOk = true;

  function nowIso() { return new Date().toISOString(); }
  function load() {
    try {
      var raw = window.localStorage.getItem(KEY);
      if (raw) { var parsed = JSON.parse(raw); if (parsed && parsed.labels) { state = parsed; } }
    } catch (e) { storageOk = false; }
  }
  function save() {
    try { window.localStorage.setItem(KEY, JSON.stringify(state)); }
    catch (e) { storageOk = false; }
    showStorage();
  }
  function showStorage() {
    var el = document.getElementById("storage");
    el.textContent = storageOk
      ? "Progress is saved in this browser automatically."
      : "This browser does not allow saving here: progress is kept only while the page is open. Export often.";
    el.className = storageOk ? "muted" : "warn";
  }
  function decode(b64) {
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
    return JSON.parse(new TextDecoder("utf-8").decode(bytes));
  }
  function el(tag, text) { var e = document.createElement(tag); if (text !== undefined) { e.textContent = text; } return e; }
  function addRow(dl, term, value) { dl.appendChild(el("dt", term)); dl.appendChild(el("dd", value)); }

  function showReveal(section) {
    var box = section.querySelector(".reveal");
    if (box.getAttribute("data-shown") === "1") { return; }
    var data = decode(box.getAttribute("data-reveal"));
    var dl = el("dl");
    addRow(dl, "Gate verdict", data.decision.toUpperCase());
    addRow(dl, "Reasons", data.reasons.length ? data.reasons.join("; ") : "(none)");
    addRow(dl, "Checks", data.checks.map(function (c) { return c.check + ": " + c.status; }).join("; ") || "(none)");
    if (data.constraints) {
      addRow(dl, "Decomposer: entity", data.constraints.entity + (data.constraints.aliases.length ? " (aliases: " + data.constraints.aliases.join(", ") + ")" : ""));
      addRow(dl, "Decomposer: attribute", data.constraints.attribute || "");
      addRow(dl, "Decomposer: qualifiers", data.constraints.qualifiers.join("; ") || "(none)");
      addRow(dl, "Decomposer: presuppositions", data.constraints.presuppositions.join("; ") || "(none)");
    } else {
      addRow(dl, "Decomposer", "no constraints recorded");
    }
    addRow(dl, "Item / condition", data.item_id + (data.condition ? " / " + data.condition : ""));
    addRow(dl, "Model / arm / dataset", data.model + " / " + data.arm + " / " + data.dataset);
    addRow(dl, "Source ids (Document 1, 2, ...)", data.source_ids.join(", ") + "; cited: " + data.cited_source_id);
    box.insertBefore(dl, box.firstChild);
    box.hidden = false;
    box.setAttribute("data-shown", "1");
  }

  function applyState(section) {
    var id = section.getAttribute("data-case");
    var entry = state.labels[id];
    var radios = section.querySelectorAll("input[type=radio]");
    var lockBtn = section.querySelector("button.lock");
    var note = section.querySelector("textarea");
    var select = section.querySelector("select");
    if (entry) {
      radios.forEach(function (r) { r.checked = (r.value === entry.label); });
      note.value = entry.note || "";
      if (entry.revealed_at) {
        radios.forEach(function (r) { r.disabled = true; });
        lockBtn.disabled = true;
        lockBtn.textContent = "Label locked";
        select.value = entry.decomposition || "not_assessed";
        showReveal(section);
        section.classList.add("done");
      }
    }
  }

  function progress() {
    var total = document.querySelectorAll("section.case").length;
    var done = Object.keys(state.labels).filter(function (k) { return state.labels[k].revealed_at; }).length;
    document.getElementById("progress").textContent = done + " of " + total + " cases labelled";
  }

  function exportLabels() {
    var labels = [];
    document.querySelectorAll("section.case").forEach(function (section) {
      var id = section.getAttribute("data-case");
      var entry = state.labels[id];
      if (entry && entry.revealed_at) {
        labels.push({
          case_id: id, label: entry.label, note: entry.note || "",
          decomposition: entry.decomposition || "not_assessed",
          labelled_at: entry.labelled_at || null, revealed_at: entry.revealed_at,
          label_entered_before_reveal: true
        });
      }
    });
    var out = { format: META.labels_format, version: META.labels_version, sample_sha256: META.sample_sha256,
                auditor: state.auditor || null, labels: labels };
    var text = JSON.stringify(out, null, 2);
    var box = document.getElementById("exportbox");
    try {
      var blob = new Blob([text], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = "audit-labels-" + META.sample_sha256.slice(0, 12) + ".json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    } catch (e) {
      box.style.display = "block";
      box.value = text;
      box.select();
    }
  }

  function importLabels(file) {
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(reader.result);
        if (data.format !== META.labels_format || data.sample_sha256 !== META.sample_sha256) {
          alert("This label file belongs to another sample; nothing was imported.");
          return;
        }
        data.labels.forEach(function (entry) {
          state.labels[entry.case_id] = { label: entry.label, note: entry.note || "",
            decomposition: entry.decomposition || "not_assessed", labelled_at: entry.labelled_at || null,
            revealed_at: entry.revealed_at || nowIso() };
        });
        if (data.auditor && !state.auditor) { state.auditor = data.auditor; }
        save();
        document.getElementById("auditor").value = state.auditor || "";
        document.querySelectorAll("section.case").forEach(applyState);
        progress();
      } catch (e) { alert("The file could not be read as a label file."); }
    };
    reader.readAsText(file);
  }

  load();
  showStorage();
  document.getElementById("auditor").value = state.auditor || "";
  document.getElementById("auditor").addEventListener("input", function (e) { state.auditor = e.target.value; save(); });
  document.getElementById("export").addEventListener("click", exportLabels);
  document.getElementById("import").addEventListener("change", function (e) { if (e.target.files[0]) { importLabels(e.target.files[0]); } });

  document.querySelectorAll("section.case").forEach(function (section) {
    var id = section.getAttribute("data-case");
    var lockBtn = section.querySelector("button.lock");
    var note = section.querySelector("textarea");
    var select = section.querySelector("select");
    section.querySelectorAll("input[type=radio]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        var entry = state.labels[id] || {};
        if (entry.revealed_at) { return; }
        entry.label = radio.value; entry.labelled_at = nowIso(); entry.note = note.value;
        state.labels[id] = entry; save();
        lockBtn.disabled = false;
      });
    });
    note.addEventListener("input", function () {
      var entry = state.labels[id];
      if (entry) { entry.note = note.value; save(); }
    });
    select.addEventListener("change", function () {
      var entry = state.labels[id];
      if (entry && entry.revealed_at) { entry.decomposition = select.value; save(); }
    });
    lockBtn.addEventListener("click", function () {
      var entry = state.labels[id];
      if (!entry || !entry.label) { return; }
      entry.revealed_at = nowIso(); entry.decomposition = entry.decomposition || "not_assessed";
      save(); applyState(section); progress();
    });
    applyState(section);
  });
  progress();
})();
"""


def _json_for_script(value: Any) -> str:
    """JSON that is safe inside a ``<script>`` element (no ``</script>`` can be formed)."""
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _encode(value: Any) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).decode("ascii")


def _e(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _case_html(case: Mapping[str, Any]) -> str:
    view = blinded_view(case)
    number = case["display_number"]
    case_id = case["case_id"]
    parts = [
        f'<section class="case" id="case-{number}" data-case="{_e(case_id)}">',
        f"<h2>Case {number} <span class=\"muted\">({_e(case_id)})</span></h2>",
        f'<p class="question">Question: {_e(view["question"])}</p>',
    ]
    for document in view["documents"]:
        text = document["text"]
        body = _e(text) if text is not None else '<span class="warn">The text of this document could not be recovered from the trace; see the full prompt below.</span>'
        parts.append(f'<div class="doc"><h3>{_e(document["label"])}</h3>{body}</div>')
    if view["prompt_fallback"] is not None:
        parts.append(
            f'<details><summary>Full prompt shown to the model</summary><div class="doc">{_e(view["prompt_fallback"])}</div></details>'
        )
    parts += [
        f"<p><strong>Candidate answer:</strong> {_e(view['value'])}</p>",
        f'<p><strong>Quote</strong> (cited from {_e(view["cited"])}):</p><div class="quote">{_e(view["quote"])}</div>',
        '<div class="labelbox">',
        "<p><strong>Is the quoted evidence about the question's entity and qualifiers?</strong></p>",
    ]
    for value in prereg.LABELS:
        parts.append(
            f'<label><input type="radio" name="label-{_e(case_id)}" value="{value}" /> {_LABEL_TEXT[value]}</label>'
        )
    parts += [
        f'<p><label for="note-{_e(case_id)}">Note (optional; for a "no", say what the evidence is about):</label></p>',
        f'<textarea id="note-{_e(case_id)}"></textarea>',
        '<p><button type="button" class="lock primary" disabled="disabled">Lock label and reveal</button></p>',
        "</div>",
        f'<div class="reveal" hidden="hidden" data-reveal="{_encode(reveal_payload(case))}">',
        f'<p><label for="dec-{_e(case_id)}">After the reveal: are the decomposer\'s constraints right for this question?</label> ',
        f'<select id="dec-{_e(case_id)}">',
    ]
    for value in prereg.DECOMPOSITION_CATEGORIES:
        parts.append(f'<option value="{value}">{_e(_DECOMPOSITION_TEXT[value])}</option>')
    parts += ["</select></p>", "</div>", "</section>"]
    return "\n".join(parts)


def render_labelling_page(sample: Mapping[str, Any], sample_sha256: str) -> str:
    """The complete HTML document for one sample."""
    meta = {
        "sample_sha256": sample_sha256,
        "labels_format": LABELS_FORMAT,
        "labels_version": LABELS_VERSION,
    }
    cases = sorted(sample["cases"], key=lambda c: c["display_number"])
    body = "\n".join(_case_html(case) for case in cases)
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            f"<title>binding-gate human audit, sample {sample_sha256[:12]}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            "<header>",
            f"<h1>binding-gate human audit <span class=\"muted\">sample {sample_sha256[:12]}, split {_e(sample['split'])}, {len(cases)} cases</span></h1>",
            '<div class="bar">',
            '<label>Auditor: <input id="auditor" type="text" size="18" /></label>',
            '<span id="progress"></span>',
            '<button type="button" id="export" class="primary">Export labels</button>',
            '<label class="muted">Import labels: <input id="import" type="file" accept=".json,application/json" /></label>',
            '<span id="storage" class="muted"></span>',
            "</div>",
            '<textarea id="exportbox" readonly="readonly"></textarea>',
            "</header>",
            "<main>",
            "<p>For each case, read the question, the documents and the candidate answer with its quote. "
            "Answer one question: is the quoted evidence about the entity and the qualifiers (time, place, "
            "version, unit, conditions) that the question asks about? Choose <em>Cannot tell</em> when the "
            "documents do not let you decide. Then press <em>Lock label and reveal</em>: your label is "
            "locked and the gate's decision is shown. Do not look at the page source; it contains the "
            "hidden information in encoded form. When you are done, press <em>Export labels</em> and send "
            "the JSON file.</p>",
            "<noscript><p class=\"warn\">This page needs JavaScript (built in, no download) to record labels. "
            "Alternatively use: python -m binding_gate.audit_tool label</p></noscript>",
            body,
            "</main>",
            f'<script type="application/json" id="audit-meta">{_json_for_script(meta)}</script>',
            f"<script>{_SCRIPT}</script>",
            "</body>",
            "</html>",
            "",
        ]
    )
