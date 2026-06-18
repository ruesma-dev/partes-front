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

  // ---------------------------------------------------------------- //
  // Filtro por columna (cliente). Cualquier tabla con una fila
  // <tr class="filter-row"> cuyas celdas lleven <input class="col-filter">
  // filtra sus filas por el texto de la columna correspondiente.
  // ---------------------------------------------------------------- //
  function _filterCellText(cell) {
    var parts = [];
    cell.querySelectorAll("input, select, textarea").forEach(function (el) {
      if (el.classList.contains("col-filter")) return;
      if (el.tagName === "SELECT") {
        var o = el.selectedOptions && el.selectedOptions[0];
        if (o) parts.push(o.textContent);
      } else {
        parts.push(el.value || "");
      }
    });
    parts.push(cell.textContent || "");
    return _norm(parts.join(" "));
  }

  function wireColumnFilters(table) {
    var frow = table.querySelector("thead tr.filter-row");
    if (!frow) return;
    var tbody = table.querySelector("tbody");
    if (!tbody) return;
    var filters = Array.prototype.slice.call(frow.querySelectorAll(".col-filter"));

    function apply() {
      var active = filters
        .map(function (inp) {
          var th = inp.closest("th");
          return { col: th ? th.cellIndex : -1, val: _norm(inp.value) };
        })
        .filter(function (f) { return f.col >= 0 && f.val !== ""; });
      Array.prototype.forEach.call(tbody.rows, function (row) {
        var show = true;
        for (var i = 0; i < active.length; i++) {
          var cell = row.cells[active[i].col];
          if (!cell || _filterCellText(cell).indexOf(active[i].val) === -1) {
            show = false; break;
          }
        }
        row.style.display = show ? "" : "none";
      });
    }
    filters.forEach(function (inp) { inp.addEventListener("input", apply); });
    frow.querySelectorAll(".filter-clear").forEach(function (btn) {
      btn.addEventListener("click", function () {
        filters.forEach(function (inp) { inp.value = ""; });
        apply();
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Ordenacion por columna al pinchar la cabecera (con flechita ▲/▼).
  // ---------------------------------------------------------------- //
  function _sortKey(cell) {
    var txt = _filterCellText(cell);
    var num = parseFloat(txt.replace(/\s/g, "").replace(",", "."));
    var isNum = txt !== "" && !isNaN(num) && /^[-+]?[\d.,\s]+$/.test(txt.trim());
    return { txt: txt, num: num, isNum: isNum };
  }

  function _sortRows(tbody, col, dir) {
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var ca = a.cells[col], cb = b.cells[col];
      var ka = ca ? _sortKey(ca) : { txt: "", isNum: false };
      var kb = cb ? _sortKey(cb) : { txt: "", isNum: false };
      var empA = ka.txt === "", empB = kb.txt === "";
      if (empA && empB) return 0;
      if (empA) return 1;          // vacios siempre al final
      if (empB) return -1;
      var cmp;
      if (ka.isNum && kb.isNum) cmp = ka.num - kb.num;
      else cmp = ka.txt < kb.txt ? -1 : (ka.txt > kb.txt ? 1 : 0);
      return cmp * dir;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  function wireSortable(table) {
    var headRow = table.querySelector("thead tr:first-child");
    var tbody = table.querySelector("tbody");
    if (!headRow || !tbody) return;
    var ths = Array.prototype.slice.call(headRow.children);
    var state = { col: -1, dir: 1 };
    ths.forEach(function (th, idx) {
      if (!th.textContent.trim() || th.classList.contains("no-sort")) return;
      th.classList.add("sortable-th");
      var arrow = document.createElement("span");
      arrow.className = "sort-arrow";
      th.appendChild(arrow);
      th.addEventListener("click", function () {
        if (state.col === idx) state.dir = -state.dir;
        else { state.col = idx; state.dir = 1; }
        ths.forEach(function (t) {
          t.classList.remove("sorted");
          var a = t.querySelector(".sort-arrow");
          if (a) a.textContent = "";
        });
        th.classList.add("sorted");
        arrow.textContent = state.dir > 0 ? " ▲" : " ▼";
        _sortRows(tbody, idx, state.dir);
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Conciliacion de trabajadores: casar (confirmar) + buscar manual.
  // ---------------------------------------------------------------- //
  function _confirmarCasado(nombre, ide, card, statusEl) {
    if (statusEl) { statusEl.textContent = "Casando…"; statusEl.className = "recon-status"; }
    return fetch("/api/conciliacion/confirmar", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ nombre_leido: nombre, ide: ide }),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.d.ok) throw new Error((res.d && res.d.error) || "Error");
        var emp = res.d.empleado || {};
        var label = (emp.codigo ? emp.codigo + " · " : "") + (emp.nombre || "");
        if (card) {
          card.classList.add("recon-done");
          card.innerHTML = '<div class="recon-head"><div class="recon-id">' +
            '<span class="recon-name">' + _esc(nombre) + '</span>' +
            '<span class="recon-meta">✓ Casado con <strong>' + _esc(label) + '</strong> · ' +
            res.d.updated + ' registro(s) actualizados</span></div>' +
            '<span class="badge ok">Hecho</span></div>';
        }
      }).catch(function (e) {
        if (statusEl) { statusEl.textContent = "✗ " + (e.message || "Error"); statusEl.className = "recon-status error"; }
      });
  }

  function _esc(s) {
    return (s || "").replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function wireConciliacion() {
    var list = document.querySelector(".recon-list");
    if (!list) return;

    // Botones "Casar" de los candidatos (delegacion).
    list.addEventListener("click", function (e) {
      var btn = e.target.closest(".btn-casar");
      if (!btn) return;
      var card = btn.closest(".recon-card");
      var statusEl = card ? card.querySelector(".recon-status") : null;
      _confirmarCasado(btn.getAttribute("data-nombre"),
        parseInt(btn.getAttribute("data-ide"), 10), card, statusEl);
    });

    // Buscadores manuales (uno por tarjeta).
    list.querySelectorAll(".manual-q").forEach(function (input) {
      var card = input.closest(".recon-card");
      var results = card.querySelector(".manual-results");
      var nombre = input.getAttribute("data-nombre");
      var timer = null;
      input.addEventListener("input", function () {
        if (timer) clearTimeout(timer);
        var q = input.value.trim();
        if (q.length < 2) { results.innerHTML = ""; return; }
        timer = setTimeout(function () {
          fetch("/api/conciliacion/buscar?q=" + encodeURIComponent(q),
            { headers: { Accept: "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              var items = (data && data.items) || [];
              if (!items.length) { results.innerHTML = '<div class="manual-empty">Sin resultados</div>'; return; }
              results.innerHTML = "";
              items.forEach(function (it) {
                var row = document.createElement("div");
                row.className = "manual-item";
                var label = (it.codigo ? it.codigo + " · " : "") + (it.nombre || "");
                row.innerHTML = '<span class="mi-score">' + it.score + '%</span>' +
                  '<span class="mi-emp">' + _esc(label) +
                  (it.dni ? ' <span class="cell-sub">DNI ' + _esc(it.dni) + '</span>' : '') + '</span>';
                var b = document.createElement("button");
                b.type = "button"; b.className = "btn primary small"; b.textContent = "Casar";
                b.addEventListener("click", function () {
                  _confirmarCasado(nombre, it.ide, card, card.querySelector(".recon-status"));
                });
                row.appendChild(b);
                results.appendChild(row);
              });
            }).catch(function () { results.innerHTML = '<div class="manual-empty">Error en la busqueda</div>'; });
        }, 250);
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Combo de EMPLEADO (autocompletar sobre el maestro activo de Sigrid)
  // para reasignar el trabajador de un registro / grupo. Busca en
  // servidor (/api/conciliacion/buscar). Al elegir, reasigna y recarga.
  // ---------------------------------------------------------------- //
  function _empReasignar(wrap, ide, label) {
    var body = { ide: ide };
    if (wrap.getAttribute("data-registro-id"))
      body.registro_id = parseInt(wrap.getAttribute("data-registro-id"), 10);
    else if (wrap.getAttribute("data-worker-key"))
      body.worker_key = wrap.getAttribute("data-worker-key");
    else if (wrap.getAttribute("data-nombre-leido"))
      body.nombre_leido = wrap.getAttribute("data-nombre-leido");
    var input = wrap.querySelector(".combo-input");
    if (input) { input.disabled = true; input.value = label; }
    fetch("/api/empleado/reasignar", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.d.ok) throw new Error((res.d && res.d.error) || "Error");
        flashEl(input, "saved");
        // El agrupado por trabajador cambia: recargamos para reflejarlo.
        window.location.reload();
      }).catch(function () {
        flashEl(input, "error");
        if (input) input.disabled = false;
      });
  }

  var EMP_URL = "/api/sigrid/empleados";
  var _empCache = null;

  function fetchEmpleados() {
    if (_empCache) return Promise.resolve(_empCache);
    return fetch(EMP_URL, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _empCache = (data && data.ok && data.items) ? data.items : [];
        return _empCache;
      }).catch(function () { _empCache = []; return _empCache; });
  }

  function empLabel(e) {
    return (e.codigo ? e.codigo + " · " : "") + (e.nombre || "");
  }

  function wireEmpleadoCombo(wrap) {
    var input = wrap.querySelector(".combo-input");
    var panel = wrap.querySelector(".combo-panel");
    if (!input || !panel) return;
    var shown = [];
    var activeIdx = -1;
    var repos = null;

    function place() {
      var r = input.getBoundingClientRect();
      panel.style.top = (r.bottom + 4) + "px";
      panel.style.left = r.left + "px";
      panel.style.minWidth = Math.max(r.width, 320) + "px";
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
        d.textContent = _empCache === null ? "Cargando empleados…" : "Sin coincidencias";
        panel.appendChild(d);
      } else {
        shown.forEach(function (e, i) {
          var it = document.createElement("div");
          it.className = "combo-item" + (i === activeIdx ? " active" : "");
          it.innerHTML = _esc(empLabel(e)) +
            (e.dni ? ' <span class="cell-sub">DNI ' + _esc(e.dni) + '</span>' : '');
          it.addEventListener("mousedown", function (ev) {
            ev.preventDefault(); _empReasignar(wrap, e.ide, empLabel(e));
          });
          panel.appendChild(it);
        });
      }
      panel.hidden = false; place();
      if (!repos) {
        repos = function (ev) {
          if (ev && ev.type === "scroll" && ev.target && panel.contains(ev.target)) return;
          close();
        };
        window.addEventListener("scroll", repos, true);
        window.addEventListener("resize", repos);
      }
    }
    function open() {
      fetchEmpleados().then(function (emps) {
        var tokens = _norm(input.value).split(/\s+/).filter(Boolean);
        shown = emps.filter(function (e) {
          if (!tokens.length) return true;
          var hay = _norm(empLabel(e) + " " + (e.dni || ""));
          return tokens.every(function (t) { return hay.indexOf(t) !== -1; });
        }).slice(0, 100);
        activeIdx = -1;
        render();
      });
    }

    input.addEventListener("focus", function () { input.select(); open(); });
    input.addEventListener("click", open);
    input.addEventListener("input", open);
    input.addEventListener("keydown", function (e) {
      if (panel.hidden) { if (e.key === "ArrowDown" || e.key === "Enter") open(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, shown.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (activeIdx >= 0 && shown[activeIdx]) _empReasignar(wrap, shown[activeIdx].ide, empLabel(shown[activeIdx]));
      } else if (e.key === "Escape") { close(); }
    });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
  }

  function wireNameEditToggles() {
    document.querySelectorAll(".name-edit-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var cell = btn.closest("td") || btn.parentElement;
        var combo = cell ? cell.querySelector(".combo-emp") : null;
        if (!combo) return;
        combo.classList.toggle("combo-emp-hidden");
        if (!combo.classList.contains("combo-emp-hidden")) {
          var inp = combo.querySelector(".combo-input");
          if (inp) inp.focus();
        }
      });
    });
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
    document.querySelectorAll("table").forEach(wireColumnFilters);
    document.querySelectorAll("table.filterable:not(.matrix)").forEach(wireSortable);
    document.querySelectorAll(".combo-emp").forEach(wireEmpleadoCombo);
    wireNameEditToggles();
    wireConciliacion();

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
