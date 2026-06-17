// static/app.js — portal de partes (sv4)
// Puebla los desplegables de codigo de hora (auxhor) desde Sigrid y
// persiste el cambio por registro via PATCH /api/registros/{id}/hora.
(function () {
  "use strict";

  var TIPOS_URL = "/api/sigrid/tipos-hora";

  function fetchTiposHora() {
    return fetch(TIPOS_URL, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .catch(function () { return { ok: false, items: [] }; });
  }

  function buildOptions(select, items, current) {
    // Agrupa Normales / Extra para facilitar la eleccion.
    var normales = items.filter(function (t) { return t.ext !== 1; });
    var extra = items.filter(function (t) { return t.ext === 1; });

    function addGroup(label, list) {
      if (!list.length) return;
      var og = document.createElement("optgroup");
      og.label = label;
      list.forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = String(t.ide);
        opt.textContent = (t.codigo ? t.codigo + " · " : "") + (t.descripcion || "");
        if (current !== "" && String(t.ide) === String(current)) opt.selected = true;
        og.appendChild(opt);
      });
      select.appendChild(og);
    }
    addGroup("Horas ordinarias", normales);
    addGroup("Horas extra", extra);
  }

  function flash(select, cls) {
    select.classList.remove("saved", "error");
    if (cls) {
      select.classList.add(cls);
      setTimeout(function () { select.classList.remove(cls); }, 1600);
    }
  }

  function patchHora(registroId, horaIde) {
    return fetch("/api/registros/" + registroId + "/hora", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ hora_ide: parseInt(horaIde, 10) }),
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function onChange(ev) {
    var select = ev.target;
    var registroId = select.getAttribute("data-registro-id");
    var value = select.value;
    var row = select.closest("tr");
    var current = row ? row.querySelector(".combo-hora-current") : null;

    if (!value) { flash(select, null); return; }
    select.disabled = true;
    patchHora(registroId, value)
      .then(function (data) {
        flash(select, "saved");
        if (current) {
          current.textContent = (data.hora_codigo || "") +
            (data.hora_descripcion ? " · " + data.hora_descripcion : "");
        }
      })
      .catch(function () { flash(select, "error"); })
      .finally(function () { select.disabled = false; });
  }

  // ---------------------------------------------------------------- //
  // Edicion in-situ: fecha + obra (nivel parte/documento) y horas (nivel
  // registro). Funciona tanto en el detalle del parte como en la tabla de
  // registros del trabajador (varias filas). Tras guardar fecha/obra se
  // propaga a las filas hermanas del MISMO documento que esten en pantalla.
  // ---------------------------------------------------------------- //
  function _norm(s) {
    return (s || "")
      .toString()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim();
  }

  function flashEl(el, cls) {
    if (!el) return;
    el.classList.remove("saved", "error");
    if (cls) {
      el.classList.add(cls);
      setTimeout(function () { el.classList.remove(cls); }, 1600);
    }
  }

  function setStatus(id, text, cls) {
    if (!id) return;
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || "";
    el.className = "edit-status" + (cls ? " " + cls : "");
    if (cls === "saved") {
      setTimeout(function () {
        if (el.textContent === text) { el.textContent = ""; el.className = "edit-status"; }
      }, 1800);
    }
  }

  function obraLabel(o) {
    return (o.codigo ? o.codigo + " · " : "") + (o.nombre || "");
  }

  // ---- Fecha (todas las .fecha-edit) ---- //
  function wireFechaInput(inp) {
    inp.addEventListener("change", function () {
      var docId = inp.getAttribute("data-document-id");
      var statusId = inp.getAttribute("data-status");
      var val = inp.value;
      if (!val) return;
      setStatus(statusId, "Guardando…", null);
      inp.disabled = true;
      fetch("/api/partes/" + docId + "/fecha", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ fecha: val }),
      }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function (data) {
        flashEl(inp, "saved");
        setStatus(statusId, "✓ Guardado", "saved");
        // Propaga a filas hermanas del mismo documento.
        document.querySelectorAll(
          '.fecha-edit[data-document-id="' + docId + '"]'
        ).forEach(function (o) { if (o !== inp) o.value = data.fecha; });
      }).catch(function () {
        flashEl(inp, "error");
        setStatus(statusId, "✗ Error", "error");
      }).finally(function () { inp.disabled = false; });
    });
  }

  // ---- Horas (todas las .horas-edit) ---- //
  function wireHorasInput(inp) {
    inp.addEventListener("change", function () {
      var regId = inp.getAttribute("data-registro-id");
      var val = inp.value;
      if (val === "") return;
      inp.disabled = true;
      fetch("/api/registros/" + regId, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ horas: val }),
      }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function () { flashEl(inp, "saved"); })
        .catch(function () { flashEl(inp, "error"); })
        .finally(function () { inp.disabled = false; });
    });
  }

  // ---- Obra: combo type-ahead local sobre el catalogo de Sigrid ---- //
  var OBRAS_URL = "/api/sigrid/obras";
  var _obrasCache = null;

  function fetchObras() {
    if (_obrasCache) return Promise.resolve(_obrasCache);
    return fetch(OBRAS_URL, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _obrasCache = (data && data.ok && data.items) ? data.items : [];
        return _obrasCache;
      })
      .catch(function () { _obrasCache = []; return _obrasCache; });
  }

  function wireObraCombo(wrap) {
    var input = wrap.querySelector(".combo-input");
    var panel = wrap.querySelector(".combo-panel");
    if (!input || !panel) return;
    var statusId = wrap.getAttribute("data-status");
    var docId = wrap.getAttribute("data-document-id");
    var shown = [];
    var activeIdx = -1;
    var repos = null;

    function place() {
      var r = input.getBoundingClientRect();
      panel.style.top = (r.bottom + 4) + "px";
      panel.style.left = r.left + "px";
      panel.style.minWidth = Math.max(r.width, 280) + "px";
      panel.style.maxWidth = "560px";
    }
    function close() {
      panel.hidden = true; activeIdx = -1;
      if (repos) {
        window.removeEventListener("scroll", repos, true);
        window.removeEventListener("resize", repos);
        repos = null;
      }
    }
    function render() {
      panel.innerHTML = "";
      if (!shown.length) {
        var d = document.createElement("div");
        d.className = "combo-msg";
        d.textContent = _obrasCache === null ? "Cargando…" : "Sin coincidencias";
        panel.appendChild(d);
      } else {
        shown.forEach(function (o, i) {
          var it = document.createElement("div");
          it.className = "combo-item" + (i === activeIdx ? " active" : "");
          it.textContent = obraLabel(o);
          it.addEventListener("mousedown", function (e) {
            e.preventDefault(); pick(o);
          });
          panel.appendChild(it);
        });
      }
      panel.hidden = false;
      place();
      if (!repos) {
        repos = function (e) {
          if (e && e.type === "scroll" && e.target && panel.contains(e.target)) return;
          close();
        };
        window.addEventListener("scroll", repos, true);
        window.addEventListener("resize", repos);
      }
    }
    function open() {
      fetchObras().then(function (obras) {
        var tokens = _norm(input.value).split(/\s+/).filter(Boolean);
        shown = obras.filter(function (o) {
          if (!tokens.length) return true;
          var nl = _norm(obraLabel(o));
          return tokens.every(function (t) { return nl.indexOf(t) !== -1; });
        }).slice(0, 200);
        activeIdx = -1;
        render();
      });
    }
    function pick(o) {
      input.value = obraLabel(o);
      close();
      setStatus(statusId, "Guardando…", null);
      fetch("/api/partes/" + docId + "/obra", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ codigo: o.codigo, ide: o.ide, nombre: o.nombre }),
      }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function () {
        flashEl(input, "saved");
        setStatus(statusId, "✓ Guardado", "saved");
        // Propaga a filas hermanas del mismo documento.
        var label = obraLabel(o);
        document.querySelectorAll(
          '.combo-obra[data-document-id="' + docId + '"]'
        ).forEach(function (w) {
          if (w === wrap) return;
          var si = w.querySelector(".combo-input");
          if (si) si.value = label;
          w.setAttribute("data-codigo", o.codigo || "");
        });
      }).catch(function () {
        flashEl(input, "error");
        setStatus(statusId, "✗ Error", "error");
      });
    }

    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", open);
    input.addEventListener("keydown", function (e) {
      if (panel.hidden) {
        if (e.key === "ArrowDown" || e.key === "Enter") open();
        return;
      }
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, shown.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
      else if (e.key === "Enter") { e.preventDefault(); if (activeIdx >= 0 && shown[activeIdx]) pick(shown[activeIdx]); }
      else if (e.key === "Escape") { close(); }
    });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Toggle del visor de PDF.
    var pdfBtn = document.getElementById("pdfToggle");
    var pdfWrap = document.getElementById("pdfWrap");
    if (pdfBtn && pdfWrap) {
      pdfBtn.addEventListener("click", function () {
        var collapsed = pdfBtn.getAttribute("data-collapsed") === "1";
        if (collapsed) {
          pdfWrap.style.display = "";
          pdfBtn.textContent = "Ocultar PDF";
          pdfBtn.setAttribute("data-collapsed", "0");
        } else {
          pdfWrap.style.display = "none";
          pdfBtn.textContent = "Mostrar PDF";
          pdfBtn.setAttribute("data-collapsed", "1");
        }
      });
    }

    document.querySelectorAll(".fecha-edit").forEach(wireFechaInput);
    document.querySelectorAll(".horas-edit").forEach(wireHorasInput);
    document.querySelectorAll(".combo-obra").forEach(wireObraCombo);

    var combos = Array.prototype.slice.call(document.querySelectorAll(".combo-hora"));
    if (!combos.length) return;

    fetchTiposHora().then(function (data) {
      var items = (data && data.items) || [];
      combos.forEach(function (select) {
        var current = select.getAttribute("data-current") || "";
        buildOptions(select, items, current);
        select.addEventListener("change", onChange);
      });
    });
  });
})();
