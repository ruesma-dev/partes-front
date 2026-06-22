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
        if (!res.d.updated && statusEl) {
          statusEl.textContent = "⚠ Casado pero 0 registros actualizados (revisa el nombre leido).";
          statusEl.className = "recon-status error";
          return;
        }
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
        if (!res.d.updated) {
          flashEl(input, "error");
          if (input) { input.disabled = false; input.value = ""; input.placeholder = "No se encontraron registros — reintenta"; }
          return;
        }
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

  // ---------------------------------------------------------------- //
  // Modal de PDF del parte. Cualquier elemento con [data-parte-pdf]
  // (= document_id) lo abre; opcional [data-parte-title] para el titulo.
  // ---------------------------------------------------------------- //
  function openPdfModal(documentId, title) {
    var modal = document.getElementById("pdf-modal");
    if (!modal) return;
    var frame = document.getElementById("pdf-modal-frame");
    var open = document.getElementById("pdf-modal-open");
    var ttl = document.getElementById("pdf-modal-title");
    var loading = document.getElementById("pdf-modal-loading");
    var url = "/partes/" + encodeURIComponent(documentId) + "/preview";
    if (ttl) ttl.textContent = title || "Parte";
    if (open) open.href = url;
    if (loading) loading.style.display = "";
    if (frame) {
      frame.onload = function () { if (loading) loading.style.display = "none"; };
      frame.src = url;
    }
    modal.hidden = false;
    document.body.classList.add("modal-open");
  }

  function closePdfModal() {
    var modal = document.getElementById("pdf-modal");
    if (!modal) return;
    var frame = document.getElementById("pdf-modal-frame");
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    if (frame) { frame.src = "about:blank"; }
  }

  function wirePdfModal() {
    var modal = document.getElementById("pdf-modal");
    if (!modal) return;
    // Disparadores en toda la pagina.
    document.body.addEventListener("click", function (e) {
      var trg = e.target.closest("[data-parte-pdf]");
      if (trg) {
        e.preventDefault();
        openPdfModal(trg.getAttribute("data-parte-pdf"), trg.getAttribute("data-parte-title"));
        return;
      }
      if (e.target.closest("[data-pdf-close]")) { e.preventDefault(); closePdfModal(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) closePdfModal();
    });
  }

  // ---------------------------------------------------------------- //
  // DESHACER: widget global. Lee el historial del servidor (persiste
  // aunque cambies de pantalla) y deshace el ultimo cambio.
  // ---------------------------------------------------------------- //
  function _undoActionLabel(a) {
    return ({
      empleado: "Trabajador", registro_edit: "Horas",
      registro_hora: "Codigo hora", parte_fecha: "Fecha",
      parte_obra: "Obra",
    })[a] || "Cambio";
  }

  function refreshUndo() {
    var widget = document.getElementById("undo-widget");
    if (!widget) return;
    fetch("/api/undo/list", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) { widget.hidden = true; return; }
        widget.hidden = false;
        var cnt = document.getElementById("undo-count");
        if (cnt) cnt.textContent = " (" + items.length + ")";
        var ul = document.getElementById("undo-list");
        if (ul) {
          ul.innerHTML = "";
          items.forEach(function (it, i) {
            var li = document.createElement("li");
            li.className = "undo-item" + (i === 0 ? " undo-next" : "");
            li.innerHTML = '<span class="undo-tag">' + _esc(_undoActionLabel(it.action)) +
              '</span> ' + _esc(it.description || "");
            ul.appendChild(li);
          });
        }
      }).catch(function () {});
  }

  function wireUndo() {
    var widget = document.getElementById("undo-widget");
    if (!widget) return;
    var btn = document.getElementById("undo-btn");
    var toggle = document.getElementById("undo-toggle");
    var panel = document.getElementById("undo-panel");
    if (btn) {
      btn.addEventListener("click", function () {
        btn.disabled = true;
        fetch("/api/undo", {
          method: "POST", headers: { Accept: "application/json" },
        }).then(function (r) { return r.json(); })
          .then(function (res) {
            if (res && res.ok) {
              window.location.reload();  // refleja el cambio revertido
            } else {
              btn.disabled = false;
              refreshUndo();
            }
          }).catch(function () { btn.disabled = false; });
      });
    }
    if (toggle && panel) {
      toggle.addEventListener("click", function () {
        panel.hidden = !panel.hidden;
        if (!panel.hidden) refreshUndo();
      });
    }
    refreshUndo();
  }

  function _postJSON(url) {
    return fetch(url, { method: "POST", headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return { ok: r.ok }; });
      })
      .catch(function () { return { ok: false }; });
  }

  function _bindClick(sel, handler) {
    document.querySelectorAll(sel).forEach(function (btn) {
      btn.addEventListener("click", function () { handler(btn); });
    });
  }

  function wireBorrado() {
    // Borrar LINEA (✕ en filas) -> soft delete -> papelera.
    _bindClick(".line-del", function (btn) {
      var row = btn.closest("[data-registro-id]");
      if (!row) return;
      var id = row.getAttribute("data-registro-id");
      if (!confirm("¿Mover esta línea a la papelera?")) return;
      btn.disabled = true;
      _postJSON("/api/registro/" + id + "/delete").then(function (d) {
        if (d && d.ok) { window.location.reload(); }
        else { btn.disabled = false; alert("No se pudo borrar la línea."); }
      });
    });
    // Borrar OBRA completa (todos sus partes).
    _bindClick("[data-del-obra]", function (btn) {
      var key = btn.getAttribute("data-del-obra");
      var label = btn.getAttribute("data-label") || "esta obra";
      var partes = btn.getAttribute("data-partes");
      var msg = "¿Mover a la papelera TODOS los partes de «" + label + "»"
        + (partes ? " (" + partes + " partes)" : "") + "?";
      if (!confirm(msg)) return;
      btn.disabled = true;
      _postJSON("/api/obra/" + encodeURIComponent(key) + "/delete").then(function (d) {
        if (d && d.ok) { window.location.href = "/obras"; }
        else { btn.disabled = false; alert("No se pudo borrar la obra."); }
      });
    });
    // Borrar PERSONA completa (todas sus líneas).
    _bindClick("[data-del-worker]", function (btn) {
      var key = btn.getAttribute("data-del-worker");
      var label = btn.getAttribute("data-label") || "esta persona";
      if (!confirm("¿Mover a la papelera TODAS las líneas de «" + label + "»?")) return;
      btn.disabled = true;
      _postJSON("/api/trabajador/" + encodeURIComponent(key) + "/delete").then(function (d) {
        if (d && d.ok) { window.location.href = "/trabajadores"; }
        else { btn.disabled = false; alert("No se pudo borrar la persona."); }
      });
    });
    // PAPELERA: restaurar / eliminar (documento y línea) + vaciar.
    _bindClick("[data-restore-doc]", function (btn) {
      btn.disabled = true;
      _postJSON("/api/documento/" + btn.getAttribute("data-restore-doc") + "/restore")
        .then(function (d) { if (d && d.ok) window.location.reload(); else btn.disabled = false; });
    });
    _bindClick("[data-hard-doc]", function (btn) {
      var label = btn.getAttribute("data-label") || "este parte";
      if (!confirm("Eliminar DEFINITIVAMENTE " + label + "?\nNo se puede deshacer.")) return;
      btn.disabled = true;
      _postJSON("/api/documento/" + btn.getAttribute("data-hard-doc") + "/hard-delete")
        .then(function (d) { if (d && d.ok) window.location.reload(); else btn.disabled = false; });
    });
    _bindClick("[data-restore-line]", function (btn) {
      btn.disabled = true;
      _postJSON("/api/registro/" + btn.getAttribute("data-restore-line") + "/restore")
        .then(function (d) { if (d && d.ok) window.location.reload(); else btn.disabled = false; });
    });
    _bindClick("[data-hard-line]", function (btn) {
      var label = btn.getAttribute("data-label") || "esta línea";
      if (!confirm("Eliminar DEFINITIVAMENTE " + label + "?\nNo se puede deshacer.")) return;
      btn.disabled = true;
      _postJSON("/api/registro/" + btn.getAttribute("data-hard-line") + "/hard-delete")
        .then(function (d) { if (d && d.ok) window.location.reload(); else btn.disabled = false; });
    });
    var vaciar = document.getElementById("papelera-vaciar");
    if (vaciar) {
      vaciar.addEventListener("click", function () {
        if (!confirm("Vaciar la papelera?\nSe eliminará DEFINITIVAMENTE todo su contenido. No se puede deshacer.")) return;
        vaciar.disabled = true;
        _postJSON("/api/papelera/vaciar")
          .then(function (d) { if (d && d.ok) window.location.reload(); else vaciar.disabled = false; });
      });
    }
  }

  // ----- Crear parte (calendario + combos) ----- //
  function _comboSimple(rootId, inputId, panelId, url, render, onPick) {
    var input = document.getElementById(inputId);
    var panel = document.getElementById(panelId);
    if (!input || !panel) return;
    var cache = null;
    function load() {
      if (cache) return Promise.resolve(cache);
      return fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) { cache = (d && d.items) || []; return cache; })
        .catch(function () { cache = []; return cache; });
    }
    function norm(s) { return (s || "").toString().toLowerCase(); }
    function show(items) {
      panel.innerHTML = "";
      if (!items.length) { panel.hidden = true; return; }
      items.slice(0, 15).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "combo-option";
        row.textContent = render(it);
        row.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          onPick(it);
          input.value = render(it);
          panel.hidden = true;
        });
        panel.appendChild(row);
      });
      panel.hidden = false;
    }
    input.addEventListener("input", function () {
      var q = norm(input.value);
      if (q.length < 1) { panel.hidden = true; return; }
      load().then(function (items) {
        show(items.filter(function (it) {
          return norm(render(it)).indexOf(q) !== -1
            || norm(it.dni).indexOf(q) !== -1
            || norm(it.codigo).indexOf(q) !== -1;
        }));
      });
    });
    input.addEventListener("focus", function () {
      if (input.value) input.dispatchEvent(new Event("input"));
    });
    document.addEventListener("click", function (ev) {
      if (!panel.hidden && !document.getElementById(rootId).contains(ev.target)) {
        panel.hidden = true;
      }
    });
  }

  function _partidaCombo(prefix, getObraIde) {
    var input = document.getElementById(prefix + "-input");
    var panel = document.getElementById(prefix + "-panel");
    var rootSel = prefix + "-combo";
    if (!input || !panel) return { reload: function () {}, clear: function () {} };
    var items = [];
    function setHidden(p) {
      document.getElementById(prefix + "-ide").value = p && p.ide != null ? p.ide : "";
      document.getElementById(prefix + "-cod").value = p ? (p.cod || "") : "";
      document.getElementById(prefix + "-res").value = p ? (p.res || "") : "";
      document.getElementById(prefix + "-capitulo").value = p ? (p.capitulo || "") : "";
    }
    function label(p) {
      return "[" + (p.capitulo || "?") + "] " + (p.cod ? p.cod + " · " : "") + (p.res || "");
    }
    function show(list) {
      panel.innerHTML = "";
      if (!list.length) { panel.hidden = true; return; }
      list.slice(0, 40).forEach(function (p) {
        var row = document.createElement("div");
        row.className = "combo-option";
        row.textContent = label(p);
        row.addEventListener("mousedown", function (ev) {
          ev.preventDefault(); setHidden(p); input.value = label(p); panel.hidden = true;
        });
        panel.appendChild(row);
      });
      panel.hidden = false;
    }
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      show(items.filter(function (p) { return label(p).toLowerCase().indexOf(q) !== -1; }));
    });
    input.addEventListener("focus", function () { if (!input.disabled) show(items); });
    document.addEventListener("click", function (ev) {
      var root = document.getElementById(rootSel);
      if (!panel.hidden && root && !root.contains(ev.target)) panel.hidden = true;
    });
    function clear() { setHidden(null); input.value = ""; }
    function reload() {
      clear();
      var obra = getObraIde();
      if (!obra) {
        items = []; input.disabled = true;
        input.placeholder = "Selecciona obra primero…";
        return;
      }
      input.disabled = true; input.placeholder = "Cargando partidas…";
      fetch("/api/sigrid/partidas?obra_ide=" + encodeURIComponent(obra),
        { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          items = (d && d.items) || [];
          input.disabled = false;
          input.placeholder = items.length
            ? ("Buscar partida… (" + items.length + ")")
            : "Esta obra no tiene partidas";
        })
        .catch(function () {
          items = []; input.disabled = false; input.placeholder = "Error al cargar partidas";
        });
    }
    return { reload: reload, clear: clear };
  }

  function wireNuevoParte() {
    var grid = document.getElementById("cal-grid");
    if (!grid) return;
    var root = document.querySelector(".nuevo-grid");
    var selected = {};            // iso -> true
    var rangeStart = null;
    var cur = new Date(root.getAttribute("data-hoy") + "T00:00:00");
    var y = cur.getFullYear(), m = cur.getMonth();
    var MESES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio",
      "Agosto","Septiembre","Octubre","Noviembre","Diciembre"];
    var DOW = ["L","M","X","J","V","S","D"];

    function iso(yy, mm, dd) {
      return yy + "-" + ("0" + (mm + 1)).slice(-2) + "-" + ("0" + dd).slice(-2);
    }
    function countSel() { return Object.keys(selected).length; }

    function renderChips() {
      var box = document.getElementById("dias-chips");
      var keys = Object.keys(selected).sort();
      document.getElementById("dias-count").textContent = keys.length;
      box.innerHTML = "";
      keys.forEach(function (k) {
        var c = document.createElement("span");
        c.className = "dia-chip";
        c.textContent = k.slice(8) + "/" + k.slice(5, 7);
        var x = document.createElement("button");
        x.type = "button"; x.textContent = "×"; x.title = "Quitar día";
        x.addEventListener("click", function () { delete selected[k]; renderCal(); });
        c.appendChild(x); box.appendChild(c);
      });
      updateBtn();
    }
    function updateBtn() {
      var obraOk = !!(document.getElementById("obra-ide").value
        || document.getElementById("obra-codigo").value);
      var empOk = !!document.getElementById("emp-nombre").value;
      document.getElementById("crear-btn").disabled =
        !(countSel() > 0 && obraOk && empOk);
    }
    function clickDay(isoStr) {
      var mode = (document.querySelector("input[name=cal-mode]:checked") || {}).value;
      if (mode === "rango") {
        if (!rangeStart) { rangeStart = isoStr; selected[isoStr] = true; }
        else {
          var a = rangeStart < isoStr ? rangeStart : isoStr;
          var b = rangeStart < isoStr ? isoStr : rangeStart;
          var d = new Date(a + "T00:00:00"), end = new Date(b + "T00:00:00");
          while (d <= end) {
            selected[iso(d.getFullYear(), d.getMonth(), d.getDate())] = true;
            d.setDate(d.getDate() + 1);
          }
          rangeStart = null;
        }
      } else {
        if (selected[isoStr]) delete selected[isoStr]; else selected[isoStr] = true;
      }
      renderCal();
    }
    function renderCal() {
      document.getElementById("cal-title").textContent = MESES[m] + " " + y;
      grid.innerHTML = "";
      DOW.forEach(function (d) {
        var h = document.createElement("div"); h.className = "cal-dow"; h.textContent = d;
        grid.appendChild(h);
      });
      var first = new Date(y, m, 1);
      var lead = (first.getDay() + 6) % 7;   // lunes=0
      for (var i = 0; i < lead; i++) {
        grid.appendChild(document.createElement("div"));
      }
      var days = new Date(y, m + 1, 0).getDate();
      for (var dd = 1; dd <= days; dd++) {
        var isoStr = iso(y, m, dd);
        var cell = document.createElement("button");
        cell.type = "button"; cell.className = "cal-day"; cell.textContent = dd;
        if (selected[isoStr]) cell.classList.add("sel");
        if (rangeStart === isoStr) cell.classList.add("range-start");
        (function (s) {
          cell.addEventListener("click", function () { clickDay(s); });
        })(isoStr);
        grid.appendChild(cell);
      }
      renderChips();
    }

    document.getElementById("cal-prev").addEventListener("click", function () {
      m--; if (m < 0) { m = 11; y--; } renderCal();
    });
    document.getElementById("cal-next").addEventListener("click", function () {
      m++; if (m > 11) { m = 0; y++; } renderCal();
    });
    document.getElementById("cal-clear").addEventListener("click", function () {
      selected = {}; rangeStart = null; renderCal();
    });
    document.querySelectorAll("input[name=cal-mode]").forEach(function (r) {
      r.addEventListener("change", function () { rangeStart = null; renderCal(); });
    });

    var partidaC = _partidaCombo("partida", function () {
      return document.getElementById("obra-ide").value;
    });

    _comboSimple("obra-combo", "obra-input", "obra-panel", "/api/sigrid/obras",
      function (o) { return (o.codigo ? o.codigo + " · " : "") + (o.nombre || ""); },
      function (o) {
        document.getElementById("obra-ide").value = o.ide != null ? o.ide : "";
        document.getElementById("obra-codigo").value = o.codigo || "";
        document.getElementById("obra-nombre").value = o.nombre || "";
        updateBtn();
        partidaC.reload();
      });
    _comboSimple("emp-combo", "emp-input", "emp-panel", "/api/sigrid/empleados",
      function (e) { return (e.nombre || "") + (e.dni ? " · " + e.dni : ""); },
      function (e) {
        document.getElementById("emp-ide").value = e.ide != null ? e.ide : "";
        document.getElementById("emp-codigo").value = e.codigo || "";
        document.getElementById("emp-nombre").value = e.nombre || "";
        document.getElementById("emp-dni").value = e.dni || "";
        updateBtn();
      });

    document.getElementById("crear-btn").addEventListener("click", function () {
      var btn = this;
      var dias = Object.keys(selected).sort();
      var payload = {
        obra_ide: document.getElementById("obra-ide").value || null,
        obra_codigo: document.getElementById("obra-codigo").value || null,
        obra_nombre: document.getElementById("obra-nombre").value || null,
        empleado_ide: document.getElementById("emp-ide").value || null,
        empleado_codigo: document.getElementById("emp-codigo").value || null,
        empleado_nombre: document.getElementById("emp-nombre").value || null,
        empleado_dni: document.getElementById("emp-dni").value || null,
        categoria: document.getElementById("categoria").value || null,
        dias: dias,
        horas_ordinaria: document.getElementById("horas-ord").value || 0,
        horas_extra: document.getElementById("horas-extra").value || 0,
        partida_ide: document.getElementById("partida-ide").value || null,
        partida_cod: document.getElementById("partida-cod").value || null,
        partida_res: document.getElementById("partida-res").value || null,
        partida_capitulo: document.getElementById("partida-capitulo").value || null
      };
      var st = document.getElementById("crear-status");
      btn.disabled = true; st.hidden = false; st.className = "form-status";
      st.textContent = "Creando…";
      fetch("/api/partes/nuevo", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) {
          st.className = "form-status ok";
          st.textContent = "Creado: " + d.lineas + " línea(s) en " + dias.length + " día(s).";
          var emp = document.getElementById("emp-ide").value;
          setTimeout(function () {
            window.location.href = emp ? ("/trabajadores/emp-" + emp) : "/partes";
          }, 700);
        } else {
          st.className = "form-status error";
          st.textContent = (d && d.error) || "No se pudo crear el parte.";
          btn.disabled = false;
        }
      }).catch(function () {
        st.className = "form-status error";
        st.textContent = "Error de red al crear el parte.";
        btn.disabled = false;
      });
    });

    renderCal();
  }

  function wireAddLine() {
    var modal = document.getElementById("addline-modal");
    if (!modal) return;
    var g = function (id) { return document.getElementById(id); };

    function setLock(prefix, locked, label, vals) {
      var lockEl = g("addline-" + prefix + "-locked");
      var combo = g("addline-" + prefix + "-combo");
      if (locked) {
        lockEl.textContent = label || "";
        lockEl.hidden = false;
        combo.hidden = true;
      } else {
        lockEl.hidden = true;
        combo.hidden = false;
        g("addline-" + prefix + "-input").value = "";
      }
      g("addline-" + prefix + "-ide").value = (vals && vals.ide) || "";
      g("addline-" + prefix + "-codigo").value = (vals && vals.codigo) || "";
      g("addline-" + prefix + "-nombre").value = (vals && vals.nombre) || "";
      if (prefix === "emp") g("addline-emp-dni").value = (vals && vals.dni) || "";
    }

    function todayISO() {
      var d = new Date();
      return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2)
        + "-" + ("0" + d.getDate()).slice(-2);
    }

    var partidaC = _partidaCombo("addline-partida", function () {
      return g("addline-obra-ide").value;
    });

    function open(btn) {
      var d = btn.dataset;
      var obraLocked = !!(d.obraCodigo || d.obraIde);
      setLock("obra", obraLocked, d.obraLabel, obraLocked
        ? { ide: d.obraIde, codigo: d.obraCodigo, nombre: d.obraNombre } : null);
      var empLocked = !!d.empNombre;
      setLock("emp", empLocked, d.empLabel, empLocked
        ? { ide: d.empIde, codigo: d.empCodigo, nombre: d.empNombre, dni: d.empDni } : null);
      g("addline-categoria").value = d.categoria || "";
      g("addline-fecha").value = d.fecha || todayISO();
      g("addline-ord").value = "8";
      g("addline-extra").value = "0";
      partidaC.reload();
      var st = g("addline-status"); st.hidden = true; st.textContent = "";
      g("addline-submit").disabled = false;
      modal.hidden = false;
      document.body.classList.add("modal-open");
    }
    function close() {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
    }

    // Combos del modal (una sola vez).
    _comboSimple("addline-obra-combo", "addline-obra-input", "addline-obra-panel",
      "/api/sigrid/obras",
      function (o) { return (o.codigo ? o.codigo + " · " : "") + (o.nombre || ""); },
      function (o) {
        g("addline-obra-ide").value = o.ide != null ? o.ide : "";
        g("addline-obra-codigo").value = o.codigo || "";
        g("addline-obra-nombre").value = o.nombre || "";
        partidaC.reload();
      });
    _comboSimple("addline-emp-combo", "addline-emp-input", "addline-emp-panel",
      "/api/sigrid/empleados",
      function (e) { return (e.nombre || "") + (e.dni ? " · " + e.dni : ""); },
      function (e) {
        g("addline-emp-ide").value = e.ide != null ? e.ide : "";
        g("addline-emp-codigo").value = e.codigo || "";
        g("addline-emp-nombre").value = e.nombre || "";
        g("addline-emp-dni").value = e.dni || "";
      });
    _bindClick("[data-add-line]", function (btn) { open(btn); });
    g("addline-close").addEventListener("click", close);
    g("addline-cancel").addEventListener("click", close);
    modal.addEventListener("click", function (ev) { if (ev.target === modal) close(); });

    g("addline-submit").addEventListener("click", function () {
      var st = g("addline-status");
      var fecha = g("addline-fecha").value;
      var obra = g("addline-obra-codigo").value || g("addline-obra-ide").value;
      var emp = g("addline-emp-nombre").value;
      if (!obra) { st.hidden = false; st.className = "form-status error"; st.textContent = "Falta la obra."; return; }
      if (!emp) { st.hidden = false; st.className = "form-status error"; st.textContent = "Falta el trabajador."; return; }
      if (!fecha) { st.hidden = false; st.className = "form-status error"; st.textContent = "Falta la fecha."; return; }
      var payload = {
        obra_ide: g("addline-obra-ide").value || null,
        obra_codigo: g("addline-obra-codigo").value || null,
        obra_nombre: g("addline-obra-nombre").value || null,
        empleado_ide: g("addline-emp-ide").value || null,
        empleado_codigo: g("addline-emp-codigo").value || null,
        empleado_nombre: g("addline-emp-nombre").value || null,
        empleado_dni: g("addline-emp-dni").value || null,
        categoria: g("addline-categoria").value || null,
        dias: [fecha],
        horas_ordinaria: g("addline-ord").value || 0,
        horas_extra: g("addline-extra").value || 0,
        partida_ide: g("addline-partida-ide").value || null,
        partida_cod: g("addline-partida-cod").value || null,
        partida_res: g("addline-partida-res").value || null,
        partida_capitulo: g("addline-partida-capitulo").value || null
      };
      var btn = this; btn.disabled = true;
      st.hidden = false; st.className = "form-status"; st.textContent = "Añadiendo…";
      fetch("/api/partes/nuevo", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (res && res.ok) {
          st.className = "form-status ok";
          st.textContent = "Añadida: " + res.lineas + " línea(s).";
          setTimeout(function () { window.location.reload(); }, 500);
        } else {
          st.className = "form-status error";
          st.textContent = (res && res.error) || "No se pudo añadir.";
          btn.disabled = false;
        }
      }).catch(function () {
        st.className = "form-status error";
        st.textContent = "Error de red.";
        btn.disabled = false;
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
    wirePdfModal();
    wireUndo();
    wireConciliacion();
    wireBorrado();
    wireNuevoParte();
    wireAddLine();

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
