"use strict";

const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  // Without this a 404 (e.g. a server still running pre-upgrade code) returns
  // {} and every caller reads it as an empty success, so nothing happens and
  // nothing is reported. Callers all check .error already.
  if (!r.ok) {
    return {
      error: body.detail || body.error || `HTTP ${r.status} ${r.statusText} — ${url}`,
      errors: body.errors,
    };
  }
  return body;
};

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $(t.dataset.tab).classList.add("active");
    if (t.dataset.tab === "groups") loadGroups();
    // one set of dictionary forms, shown on whichever of the two tabs is open
    if (t.dataset.tab === "dictionaries") { $("dict_forms_home").append($("dict_forms")); loadDictionaries(); }
    if (t.dataset.tab === "columns") $("col_dicts_body").append($("dict_forms"));
    if (t.dataset.tab === "columns") { loadMappingSummary(); loadDictionaries(); }
    if (t.dataset.tab === "migrate") { refreshCompare(); refreshDriftPanel(); refreshCheckpointPanel(); }
    if (t.dataset.tab === "history") loadHistory();
  };
});

function setStatus(el, msg, ok) {
  el.textContent = msg;
  el.className = "status " + (ok === true ? "ok" : ok === false ? "err" : "");
}

// ---------- config ----------
async function loadConfig() {
  const c = await api("/api/config");
  if (c.oracle) {
    o_host.value = c.oracle.host || "";
    o_port.value = c.oracle.port || 1521;
    o_service.value = c.oracle.service || "";
    o_user.value = c.oracle.user || "";
    o_schema.value = c.oracle.schema || "";
  }
  if (c.pg) {
    p_host.value = c.pg.host || "";
    p_port.value = c.pg.port || 5432;
    p_dbname.value = c.pg.dbname || "";
    p_user.value = c.pg.user || "";
    p_target_schema.value = c.pg.target_schema || "public";
  }
  selection = c.selection || [];
  // a fresh install: offer the settings from an older copy or another PC
  $("first_run").hidden = !!(c.oracle || c.pg) || allSets.length > 1;
}

$("o_save").onclick = async () => {
  const body = {
    host: o_host.value, port: +o_port.value, service: o_service.value,
    user: o_user.value, password: o_password.value, schema: o_schema.value || null,
  };
  const r = await api("/api/config/oracle", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  setStatus($("o_status"), r.ok ? "Saved" : "Error", r.ok);
};

$("p_save").onclick = async () => {
  const body = {
    host: p_host.value, port: +p_port.value, dbname: p_dbname.value,
    user: p_user.value, password: p_password.value, target_schema: p_target_schema.value || "public",
  };
  const r = await api("/api/config/pg", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  setStatus($("p_status"), r.ok ? "Saved" : "Error", r.ok);
};

$("o_test").onclick = async () => {
  setStatus($("o_status"), "Testing…");
  const r = await api("/api/test/oracle", { method: "POST" });
  setStatus($("o_status"), r.ok ? "Connected" : r.error, r.ok);
};
$("p_test").onclick = async () => {
  setStatus($("p_status"), "Testing…");
  const r = await api("/api/test/pg", { method: "POST" });
  setStatus($("p_status"), r.ok ? "Connected" : r.error, r.ok);
};

// ---------- tables ----------
let allTables = [];
let selection = [];
let rowCounts = {};

function renderTables() {
  const f = $("filter").value.toUpperCase();
  const list = $("table_list");
  list.innerHTML = "";
  allTables.filter((t) => t.includes(f)).forEach((t) => {
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = t;
    cb.checked = selection.includes(t);
    cb.onchange = () => {
      if (cb.checked) selection.push(t);
      else selection = selection.filter((x) => x !== t);
    };
    lbl.appendChild(cb);
    const info = document.createElement("span");
    info.className = "table-info";
    const name = document.createElement("span");
    name.className = "table-name";
    name.textContent = t;
    info.appendChild(name);
    const rc = rowCounts[t];
    if (rc !== undefined) {
      const badge = document.createElement("span");
      badge.className = "badge muted";
      badge.textContent = rc === null ? "no stats" : `~${fmt(rc)} rows`;
      info.appendChild(badge);
    }
    lbl.appendChild(info);
    list.appendChild(lbl);
  });
}

$("load_tables").onclick = async () => {
  setStatus($("tables_status"), "Loading…");
  const r = await api("/api/oracle/tables");
  if (r.error) return setStatus($("tables_status"), r.error, false);
  allTables = r.tables;
  rowCounts = {};
  renderTables();
  setStatus($("tables_status"), `${allTables.length} tables`, true);
};
$("filter").oninput = renderTables;
$("select_all").onclick = () => { selection = [...allTables]; renderTables(); };
$("select_none").onclick = () => { selection = []; renderTables(); };
$("show_row_counts").onclick = async () => {
  if (!allTables.length) return setStatus($("tables_status"), "Load tables first", false);
  setStatus($("tables_status"), "Fetching row count estimates…");
  const r = await api("/api/oracle/row-count-estimates");
  if (r.error) return setStatus($("tables_status"), r.error, false);
  rowCounts = r.counts;
  renderTables();
  setStatus($("tables_status"), "Row counts are estimates from table statistics (may be stale)", true);
};
$("save_selection").onclick = async () => {
  const r = await api("/api/selection", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: selection }),
  });
  setStatus($("tables_status"), `Saved ${r.count} tables`, true);
};

// ---------- column mapping ----------
let currentColTable = null;
let pgTypeChoices = [];
let dictNames = [];  // dictionaries, then Postgres lookups
let lookupDefs = {};  // name -> {table, match, value}

// options for a column's Dictionary select; keeps a saved choice that was since deleted
function fillDictSelect(sel, value) {
  sel.replaceChildren(new Option("—", ""), ...[...new Set([...dictNames, value].filter(Boolean))]
    .map((n) => new Option(lookupDefs[n] ? `${n} (lookup)` : n, n)));
  sel.value = value || "";
  sel.onchange?.();
}

// Where a dictionary or lookup is used: every saved mapping column that goes
// through it (highlighted), or a note that none does.
function usedBy(item, users) {
  const line = (text, cls) => Object.assign(document.createElement("span"), { className: cls, textContent: text });
  if (!users?.length) return line("Not used by any mapping", "used-by unused");
  item.classList.add("used");
  return line("Used by:\n" + users.join("\n"), "used-by");
}

let dictData = { dictionaries: [], lookups: {}, used_by: {} };  // the last /api/dictionaries

// Saved mapping columns using `name`, all or only those of mapping `table`
const usersOf = (name, table) => (dictData.used_by[name] || []).filter((u) => !table || u.startsWith(table + "."));

// One dictionary as a card: name and size, where it's used (only by `table`,
// when given), View / Download / Delete. A click picks it for the upload form.
function dictItem(d, table) {
  const item = document.createElement("div");
  item.className = "mapping-item row";
  item.style.margin = "0";
  const del = Object.assign(document.createElement("button"), { className: "danger", textContent: "Delete" });
  del.onclick = async () => {
    if (!confirm(`Delete dictionary ${d.name}? Columns using it will fail to sync until you pick another.`)) return;
    const x = await api("/api/dictionaries/" + encodeURIComponent(d.name), { method: "DELETE" });
    if (x.error) return setStatus($("dict_status"), x.error, false);
    loadDictionaries();
  };
  const csv = Object.assign(document.createElement("a"), {
    className: "btn", textContent: "Download",
    href: "/api/dictionaries/" + encodeURIComponent(d.name) + "/export",
  });
  const view = Object.assign(document.createElement("button"), { className: "secondary", textContent: "View" });
  view.onclick = () => openDictView(d.name);
  item.append(Object.assign(document.createElement("span"), {
    className: "mapping-item-name", textContent: `${d.name} (${d.entries} entries)`, style: "flex: 1 1 100%",
  }), usedBy(item, usersOf(d.name, table)), view, csv, del);
  item.onclick = (e) => { if (e.target === item || e.target.tagName === "SPAN") $("dict_name").value = d.name; };
  return item;
}

// One Postgres lookup as a card: its link (table, match and value columns),
// where it's used (only by `table`, when given), Delete. A click loads it into
// the lookup form.
function lookupItem(name, table) {
  const lk = dictData.lookups[name];
  const item = document.createElement("div");
  item.className = "mapping-item row";
  item.style.margin = "0";
  const del = Object.assign(document.createElement("button"), { className: "danger", textContent: "Delete" });
  del.onclick = async () => {
    if (!confirm(`Delete lookup ${name}? Columns using it will fail to sync until you pick another.`)) return;
    const x = await api("/api/lookups/" + encodeURIComponent(name), { method: "DELETE" });
    if (x.error) return setStatus($("lk_status"), x.error, false);
    loadDictionaries();
  };
  item.append(Object.assign(document.createElement("span"), {
    className: "mapping-item-name", style: "flex: 1",
    textContent: `${name}: ${lk.table}.${lk.value} where ${lk.match.join(", ")} match`,
  }), del, usedBy(item, usersOf(name, table)));
  item.onclick = (e) => {
    if (e.target !== item && e.target.tagName !== "SPAN") return;
    $("lk_name").value = name;
    $("lk_table").value = lk.table;
    $("lk_match").value = lk.match.join(", ");
    $("lk_value").value = lk.value;
  };
  return item;
}

async function loadDictionaries() {
  const r = await api("/api/dictionaries");
  if (r.error) return setStatus($("dict_status"), r.error, false);
  dictData = { dictionaries: r.dictionaries, lookups: r.lookups, used_by: r.used_by || {} };
  lookupDefs = r.lookups;
  dictNames = [...r.dictionaries.map((d) => d.name), ...Object.keys(lookupDefs).sort()];
  document.querySelectorAll("#col_grid .col-dict").forEach((s) => fillDictSelect(s, s.value));
  renderDicts();
  renderTableDicts();
}

// At most `n` items in view, the rest scroll. Measured, so items of any
// height fit; a list on a hidden tab is measured when the tab opens.
function capList(list, n = 5) {
  list.style.maxHeight = "";
  if (list.children.length <= n || !list.offsetHeight) return;
  list.style.maxHeight = `${list.children[n - 1].getBoundingClientRect().bottom - list.getBoundingClientRect().top}px`;
}

// The Mappings tab's Dictionaries column: the dictionaries and lookups the
// open table's saved mapping uses.
function renderTableDicts() {
  const t = currentColTable;
  const mine = (name) => usersOf(name, t).length > 0;
  const items = !t ? [] : [
    ...dictData.dictionaries.filter((d) => mine(d.name)).map((d) => dictItem(d, t)),
    ...Object.keys(dictData.lookups).sort().filter(mine).map((n) => lookupItem(n, t)),
  ];
  $("col_dict_list").replaceChildren(...items);
  $("col_dicts_note").textContent = !t ? "Pick a table to see the dictionaries it uses."
    : items.length ? `Used by ${t}'s saved mapping:`
    : `${t}'s saved mapping uses no dictionary yet. Pick one in a column's Dictionary list, or add one below.`;
  capList($("col_dict_list"));
  colDictsToggleLabel(items.length);
}

// The Mappings tab's Dictionaries column can fold into a slim strip to give
// the column grid room. Remembered per browser; works without storage too.
const DICTS_FOLDED = "dataflow.dictsCollapsed";
let colDictsCount = 0;  // dictionaries the open table uses, shown on the folded strip

function colDictsToggleLabel(count = colDictsCount) {
  colDictsCount = count;
  const folded = $("columns").classList.contains("dicts-collapsed");
  $("col_dicts_toggle").textContent = folded ? `Dictionaries${count ? ` · ${count}` : ""}` : "Hide";
  $("col_dicts_toggle").title = folded ? "Show the Dictionaries column" : "Hide the Dictionaries column";
}

function foldColDicts(folded) {
  $("columns").classList.toggle("dicts-collapsed", folded);
  try { localStorage.setItem(DICTS_FOLDED, folded ? "1" : ""); } catch { /* storage blocked: just not remembered */ }
  colDictsToggleLabel();
  if (!folded) capList($("col_dict_list"));  // measured now that it's visible again
}

$("col_dicts_toggle").onclick = () => foldColDicts(!$("columns").classList.contains("dicts-collapsed"));
try { foldColDicts(localStorage.getItem(DICTS_FOLDED) === "1"); } catch { foldColDicts(false); }

// The Dictionaries tab's cards, filtered by the search box: name, the Oracle
// and Postgres columns and tables using it, and a lookup's own table and columns.
function renderDicts() {
  const f = $("dict_search").value.trim().toLowerCase();
  const hit = (...texts) => !f || texts.flat().some((t) => t.toLowerCase().includes(f));
  const used = (name) => dictData.used_by[name] || [];
  const dicts = dictData.dictionaries.filter((d) => hit(d.name, used(d.name)));
  const lookups = Object.keys(dictData.lookups).sort().filter((n) => {
    const lk = dictData.lookups[n];
    return hit(n, used(n), lk.table, lk.match, lk.value);
  });
  const fill = (box, items, none) => box.replaceChildren(...(items.length ? items
    : [Object.assign(document.createElement("p"), { className: "status", textContent: none })]));
  fill($("dict_list"), dicts.map((d) => dictItem(d)), f ? "No dictionary matches." : "No dictionaries yet - add one on the right.");
  fill($("lk_list"), lookups.map((n) => lookupItem(n)), f ? "No lookup matches." : "No lookups yet - add one on the right.");
  const total = dictData.dictionaries.length + Object.keys(dictData.lookups).length;
  $("dict_count").textContent = f ? `${dicts.length + lookups.length} of ${total} match` : `${total} in all`;
}

$("dict_search").oninput = renderDicts;

// body: the file (or pasted text) as is; the server reads .xlsx or delimited text by filename
async function sendDictRows(body, filename, replace) {
  const name = $("dict_name").value.trim();
  if (!name) { setStatus($("dict_status"), "Name the dictionary, or pick one below to update it", false); return false; }
  if (replace && dictNames.includes(name) && !confirm(`Replace every entry in ${name}?`)) return false;
  const r = await api(`/api/dictionaries/${encodeURIComponent(name)}?filename=${encodeURIComponent(filename)}&replace=${replace}`,
    { method: "POST", body });
  if (r.error) { setStatus($("dict_status"), r.error, false); return false; }
  setStatus($("dict_status"), `${name}: ${fmt(r.added)} added, ${fmt(r.updated)} updated — ${fmt(r.total)} entries`, true);
  loadDictionaries();
  return true;
}

// ---------- dictionary viewer (popup) ----------
const DICT_VIEW_MAX = 500;  // rows drawn at once; search narrows the rest
let dictViewEntries = [];
let dictViewName = null;

async function loadDictView() {
  const r = await api("/api/dictionaries/" + encodeURIComponent(dictViewName));
  if (r.error) return r;
  dictViewEntries = r.entries;
  $("dict_view_title").textContent = `${dictViewName} (${fmt(r.entries.length)} entries)`;
  renderDictView();
  return r;
}

async function openDictView(name) {
  dictViewName = name;
  $("dict_view_search").value = "";
  setStatus($("dict_view_status"), "");
  const r = await loadDictView();
  if (r.error) return setStatus($("dict_status"), r.error, false);
  $("dict_view").showModal();
  $("dict_view_search").focus();
}

// one entry, through the same add/update route as uploads
$("dict_view_add").onsubmit = async (e) => {
  e.preventDefault();
  const [o, p] = [$("dict_view_old").value.trim(), $("dict_view_new").value.trim()];
  if (!o || !p) return setStatus($("dict_view_status"), "Fill in both values", false);
  const csv = (v) => `"${v.replaceAll('"', '""')}"`;  // values may hold commas or quotes
  const r = await api(`/api/dictionaries/${encodeURIComponent(dictViewName)}?filename=rows.csv`,
    { method: "POST", body: `oracle,postgres\n${csv(o)},${csv(p)}\n` });
  if (r.error) return setStatus($("dict_view_status"), r.error, false);
  setStatus($("dict_view_status"),
    r.added ? `Added ${o} → ${p}` : r.updated ? `Updated ${o} → ${p}` : `${o} → ${p} was already there`, true);
  $("dict_view_old").value = $("dict_view_new").value = "";
  $("dict_view_old").focus();
  await loadDictView();
  loadDictionaries();  // the entry count in the list
};

function renderDictView() {
  const f = $("dict_view_search").value.trim().toLowerCase();
  const hits = f ? dictViewEntries.filter(([o, p]) => o.toLowerCase().includes(f) || p.toLowerCase().includes(f))
                 : dictViewEntries;
  $("dict_view_rows").replaceChildren(...hits.slice(0, DICT_VIEW_MAX).map(([o, p]) => {
    const tr = document.createElement("tr");
    tr.append(...[o, p].map((v) => Object.assign(document.createElement("td"), { textContent: v })));
    return tr;
  }));
  $("dict_view_count").textContent = hits.length > DICT_VIEW_MAX
    ? `Showing the first ${fmt(DICT_VIEW_MAX)} of ${fmt(hits.length)}${f ? " matches" : ""} - search to narrow it down.`
    : f ? `${fmt(hits.length)} match${hits.length === 1 ? "" : "es"}` : "";
}

$("dict_view_search").oninput = renderDictView;
$("dict_view_close").onclick = () => $("dict_view").close();
// a click on the dimmed backdrop lands on the dialog itself
$("dict_view").onclick = (e) => { if (e.target === $("dict_view")) $("dict_view").close(); };

$("lk_save").onclick = async () => {
  const name = $("lk_name").value.trim();
  if (!name) return setStatus($("lk_status"), "Name the lookup, or pick one below to edit it", false);
  const r = await api("/api/lookups/" + encodeURIComponent(name), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ table: $("lk_table").value, match: $("lk_match").value.split(","), value: $("lk_value").value }),
  });
  if (r.error) return setStatus($("lk_status"), r.error, false);
  setStatus($("lk_status"), `${name} saved`, true);
  loadDictionaries();
};

$("dict_upload").onclick = async () => {
  const file = $("dict_file").files[0];
  if (!file) return setStatus($("dict_status"), "Pick an Excel or CSV file", false);
  if (await sendDictRows(file, file.name, $("dict_replace").checked)) {
    $("dict_file").value = "";
    $("dict_replace").checked = false;
  }
};

$("dict_add_rows").onclick = async () => {
  const text = $("dict_rows").value.trim();
  if (!text) return setStatus($("dict_status"), "Type or paste some rows first", false);
  const delim = ["\t", ";", ","].find((c) => text.includes(c)) || ",";
  if (await sendDictRows(`oracle${delim}postgres\n${text}\n`, "rows.csv", false)) $("dict_rows").value = "";
};

$("dict_delete_rows").onclick = async () => {
  const name = $("dict_name").value.trim();
  if (!name) return setStatus($("dict_status"), "Pick the dictionary below first", false);
  // first column of each line: the old ID
  const oracle = $("dict_rows").value.split("\n").map((l) => l.split(/[\t;,]/)[0].trim()).filter(Boolean);
  if (!oracle.length) return setStatus($("dict_status"), "List the old IDs to delete, one per line", false);
  if (!confirm(`Delete ${oracle.length} entr${oracle.length === 1 ? "y" : "ies"} from ${name}?`)) return;
  const r = await api(`/api/dictionaries/${encodeURIComponent(name)}/delete-entries`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ oracle }),
  });
  if (r.error) return setStatus($("dict_status"), r.error, false);
  const missing = r.not_found.length ? ` — not in it: ${r.not_found.join(", ")}` : "";
  setStatus($("dict_status"), `${name}: ${fmt(r.removed)} deleted, ${fmt(r.total)} entries left${missing}`, r.not_found.length ? null : true);
  if (!r.not_found.length) $("dict_rows").value = "";
  loadDictionaries();
};

function typeLabel(c) {
  if (c.dtype === "NUMBER") {
    if (c.prec == null) return "NUMBER";
    return c.scale ? `NUMBER(${c.prec},${c.scale})` : `NUMBER(${c.prec})`;
  }
  return c.dtype;
}

function mappingBadges(row) {
  const badges = [];
  if (row.error) return [`<span class="badge err">${row.error}</span>`];
  if (row.renamed) badges.push(`<span class="badge">${row.renamed} renamed</span>`);
  if (row.skipped) badges.push(`<span class="badge warn">${row.skipped} skipped</span>`);
  if (row.drift) badges.push(`<span class="badge err">mapping changed</span>`);
  if (!row.renamed && !row.skipped) badges.push('<span class="badge muted">default</span>');
  if (row.fast_load) badges.push('<span class="badge">fast load</span>');
  if (row.group)
    badges.push(
      `<span class="badge">${row.group_mode} → ${row.group}</span>` +
      (row.group_with.length ? ` <span class="badge muted">with ${row.group_with.join(", ")}</span>` : "")
    );
  return badges;
}

let summaryReq = 0;
async function loadMappingSummary() {
  const list = $("mapping_summary");
  list.innerHTML = '<p class="status">Loading…</p>';
  const q = selection.length ? "?tables=" + encodeURIComponent(selection.join(",")) : "";
  // each call hits Oracle per table and can be slow; a reply that comes back
  // after a newer request (e.g. one fired before a delete) must not repaint
  const req = ++summaryReq;
  const r = await api("/api/mappings" + q);
  if (req !== summaryReq) return;
  if (r.error) {
    list.innerHTML = `<p class="status err">${r.error}</p>`;
    return;
  }
  if (!r.rows.length) {
    list.innerHTML = '<p class="status">No tables selected yet. Pick tables on the Tables tab and save your selection.</p>';
    return;
  }
  list.innerHTML = "";
  r.rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "mapping-item" + (row.table === currentColTable ? " active" : "");
    item.innerHTML =
      `<div class="mapping-item-name">${row.table} &rarr; ${row.pg_table}</div>` +
      `<div class="mapping-item-badges">${mappingBadges(row).join(" ")}</div>`;
    item.onclick = () => loadColumns(row.table);
    list.appendChild(item);
  });
}

function renderColumnGrid(cols, constants = []) {
  const tb = $("col_grid").querySelector("tbody");
  tb.innerHTML = "";
  cols.filter((c) => !c.pg_only).forEach((c) => tb.appendChild(columnRow(c)));
  // Postgres-only rows go last: their Oracle-column list is read from the rows above
  cols.filter((c) => c.pg_only).forEach((c) => tb.appendChild(pgOnlyRow(c)));
  constants.forEach((k) => tb.appendChild(pgOnlyRow({ name: "", target: k.target, if_null: k.value })));
  syncUseAll();
}

// A column of the existing table that no Oracle column feeds, added with
// "Add to Sync": a fixed value, or a dictionary / lookup applied to the value
// of the Oracle column picked here (saved as one more destination of it).
function pgOnlyRow(p) {
  const tr = columnRow({ name: p.name || "", target: p.target, skip: false, nullable: true,
                         dictionary: p.dictionary, lookup_with: p.lookup_with, if_null: p.if_null });
  tr.classList.add("col-pgonly");
  tr.dataset.col = "";
  const [tdName, tdType, , tdPgType, , , tdUse] = tr.children;
  const target = tr.querySelector(".col-target");
  const dict = tr.querySelector(".col-dict");
  const fill = tr.querySelector(".col-ifnull");

  const key = document.createElement("select");
  key.className = "col-pgkey";
  const oracleCols = [...new Set([...document.querySelectorAll("#col_grid tbody tr:not(.col-pgonly)")]
    .map((r) => r.dataset.col))];
  key.replaceChildren(new Option("Fixed value", ""), ...oracleCols.map((n) => new Option(`Look up ${n}`, n)));
  key.value = p.name || "";
  key.title = "Fixed value: the same value in every row. Look up: the dictionary or lookup is applied to this Oracle column's value";
  key.onchange = () => {
    target.dataset.name = key.value;
    dict.disabled = !key.value;
    if (!key.value) { dict.value = ""; dict.onchange(); }
    fill.placeholder = key.value ? "if that's empty" : "value";
    fill.title = key.value ? "Loaded when the Oracle value is empty" : "Loaded into every row";
  };
  key.onchange();

  const remove = Object.assign(document.createElement("button"), { type: "button", textContent: "×",
                                                                    title: "Don't fill this column" });
  remove.onclick = () => { tr.remove(); checkTargets(); };
  tdName.replaceChildren(Object.assign(document.createElement("span"), { className: "pgonly-label",
                                                                         textContent: "Postgres only " }), remove, key);
  tdType.textContent = "—";
  tdPgType.querySelector(".col-pgtype").hidden = true;  // the column exists: its type is the table's
  tdUse.querySelector(".col-use").hidden = true;  // always used; × removes it
  return tr;
}

let filledCols = new Set();  // Oracle columns of the open table that hold data

// Highlights those columns; the check scans the table, so it lands after the grid
async function loadFilled(table) {
  const note = $("col_filled_note");
  setStatus(note, "Checking which columns have data…");
  const r = await api("/api/table/" + encodeURIComponent(table) + "/filled");
  if (table !== currentColTable) return;  // another table was opened meanwhile
  if (r.error) return setStatus(note, "Could not check for data: " + r.error, false);
  filledCols = new Set(r.filled);
  document.querySelectorAll("#col_grid tbody tr").forEach((tr) =>
    tr.firstElementChild.classList.toggle("has-data", filledCols.has(tr.dataset.col)));
  setStatus(note, `${filledCols.size} of ${new Set([...document.querySelectorAll("#col_grid tbody tr:not(.col-pgonly)")].map((tr) => tr.dataset.col)).size} columns have data (highlighted).`);
}

function columnRow(c) {
  const tr = document.createElement("tr");

  // "+" gives this Oracle column another Postgres destination (a copy row,
  // saved as the same name listed again); "×" removes a copy
  const tdName = document.createElement("td");
  tdName.textContent = (c.copy ? "↳ " : "") + c.name + " ";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.textContent = c.copy ? "×" : "+";
  copyBtn.title = c.copy ? "Remove this destination" : "Also load this column into another Postgres column";
  copyBtn.onclick = () => {
    if (c.copy) {
      tr.remove();
      syncUseAll();
      checkTargets();
      return;
    }
    let last = tr;  // after the last of this column's copies
    while (last.nextElementSibling && last.nextElementSibling.classList.contains("col-copy")) last = last.nextElementSibling;
    const n = [...tr.parentNode.querySelectorAll("tr")].filter((r) => r.dataset.col === c.name).length + 1;
    last.after(columnRow({ ...c, copy: true, skip: false, target: `${c.name.toLowerCase()}_${n}`,
                           pg_type_override: null, dictionary: null, lookup_with: null, if_null: null }));
    fillPgColSelects();
    syncUseAll();
    checkTargets();
  };
  tdName.appendChild(copyBtn);
  tdName.classList.toggle("has-data", filledCols.has(c.name));
  tr.dataset.col = c.name;
  tr.classList.toggle("col-copy", !!c.copy);

  const tdType = document.createElement("td");
  tdType.textContent = typeLabel(c);

  const tdTarget = document.createElement("td");
  const targetInput = document.createElement("input");
  targetInput.value = c.target;
  targetInput.dataset.name = c.name;
  targetInput.dataset.orig = c.target;
  targetInput.className = "col-target";
  targetInput.disabled = c.skip;
  targetInput.setAttribute("list", "pg_cols_list");
  targetInput.oninput = checkTargets;
  // shown instead of the text box when the target table already exists
  const pgColSelect = document.createElement("select");
  pgColSelect.className = "col-pgcol";
  pgColSelect.hidden = true;
  pgColSelect.disabled = c.skip;
  pgColSelect.onchange = () => { targetInput.value = pgColSelect.value; checkTargets(); };
  tdTarget.append(targetInput, pgColSelect);

  const tdPgType = document.createElement("td");
  const typeSelect = document.createElement("select");
  typeSelect.className = "col-pgtype";
  typeSelect.dataset.name = c.name;
  typeSelect.disabled = c.skip;
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = `Default (${c.default_pg_type})`;
  typeSelect.appendChild(defaultOpt);
  pgTypeChoices.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    typeSelect.appendChild(opt);
  });
  typeSelect.value = c.pg_type_override || "";
  tdPgType.appendChild(typeSelect);

  const tdDict = document.createElement("td");
  const dictSelect = document.createElement("select");
  dictSelect.className = "col-dict";
  dictSelect.disabled = c.skip;
  // a lookup matching on several columns takes the others from this row's Oracle columns
  const withInput = document.createElement("input");
  withInput.className = "col-lkwith";
  withInput.value = (c.lookup_with || []).join(", ");
  withInput.disabled = c.skip;
  dictSelect.onchange = () => {
    const more = (lookupDefs[dictSelect.value]?.match || []).slice(1);
    withInput.hidden = !more.length;
    withInput.placeholder = `Oracle column(s) for ${more.join(", ")}`;
    withInput.title = `Matched against ${more.join(", ")}, in that order`;
  };
  fillDictSelect(dictSelect, c.dictionary);
  tdDict.append(dictSelect, withInput);

  const tdIfNull = document.createElement("td");
  const ifNullInput = document.createElement("input");
  ifNullInput.className = "col-ifnull";
  ifNullInput.value = c.if_null ?? "";
  ifNullInput.placeholder = c.nullable ? "" : "required";
  ifNullInput.title = "Loaded instead of NULL when the Oracle value is empty";
  ifNullInput.disabled = c.skip;
  tdIfNull.appendChild(ifNullInput);

  const tdUse = document.createElement("td");
  const useCb = document.createElement("input");
  useCb.type = "checkbox";
  useCb.checked = !c.skip;
  useCb.className = "col-use";
  useCb.dataset.name = c.name;
  useCb.onchange = () => {
    targetInput.disabled = !useCb.checked;
    pgColSelect.disabled = !useCb.checked;
    typeSelect.disabled = !useCb.checked;
    dictSelect.disabled = !useCb.checked;
    withInput.disabled = !useCb.checked;
    ifNullInput.disabled = !useCb.checked;
    syncUseAll();
    checkTargets();
  };
  tdUse.appendChild(useCb);

  tr.append(tdName, tdType, tdTarget, tdPgType, tdDict, tdIfNull, tdUse);
  return tr;
}

// header checkbox: ticked when every column is used, dashed when some are
function syncUseAll() {
  const boxes = [...document.querySelectorAll("#col_grid tr:not(.col-pgonly) .col-use")];
  const on = boxes.filter((b) => b.checked).length;
  $("col_use_all").checked = boxes.length > 0 && on === boxes.length;
  $("col_use_all").indeterminate = on > 0 && on < boxes.length;
}

$("col_use_all").onchange = () => {
  document.querySelectorAll("#col_grid tr:not(.col-pgonly) .col-use").forEach((b) => {
    b.checked = $("col_use_all").checked;
    b.onchange();
  });
};

async function loadColumns(table) {
  currentColTable = table;
  renderTableDicts();
  // extra mappings are keyed "ORACLE_TABLE:n"
  const [oracleTable, n] = table.split(":");
  $("col_table_title").textContent = n ? `${oracleTable} (mapping ${n})` : table;
  $("col_new").disabled = false;
  $("col_delete").hidden = false;
  setStatus($("columns_status"), "Loading…");
  const r = await api("/api/table/" + encodeURIComponent(table) + "/columns");
  if (r.error) {
    setStatus($("columns_status"), r.error, false);
    $("col_grid").querySelector("tbody").innerHTML = "";
    $("col_pg_table").value = "";
    $("col_pg_table").disabled = true;
    $("col_fast_load").checked = false;
    $("col_fast_load").disabled = true;
    $("col_upsert").checked = false;
    $("col_upsert").disabled = true;
    $("col_save").disabled = true;
    return;
  }
  $("col_pg_table").value = r.pg_table;
  $("col_pg_table").disabled = false;
  $("col_fast_load").checked = !!r.fast_load;
  $("col_fast_load").disabled = false;
  $("col_upsert").checked = !!r.upsert;
  $("col_upsert").disabled = false;
  $("col_save").disabled = false;
  pgTypeChoices = r.pg_type_choices || [];
  dictNames = r.dictionaries || [];
  lookupDefs = r.lookups || {};
  // a never-saved mapping opens with nothing ticked: pick only what you need
  // (until it's saved, a sync still takes every column)
  filledCols = new Set();
  loadFilled(table);  // not awaited: it scans the table
  renderColumnGrid(r.saved ? r.columns : r.columns.map((c) => ({ ...c, skip: true })), r.constants || []);
  setStatus($("columns_status"),
    r.saved ? "" : "Not saved yet: tick the columns you need, then Save. Until then a sync takes every column.", null);
  await loadPgTables(true);  // fresh each time: the Postgres connection may have changed
  $("col_pg_pick").replaceChildren(pgTablesError
    ? Object.assign(document.createElement("p"), { className: "status err", textContent: "Postgres: " + pgTablesError })
    : pgTablePicker($("col_pg_table")));
  await onPgTableChange();
  loadMappingSummary();
}

// ----- mapping onto an existing Postgres table -----
let pgTables = null;   // existing tables in the target schema, fetched once
let pgCols = null;     // columns of the chosen table when it already exists

async function loadPgTables(force) {
  if (pgTables && !force) return;
  const r = await api("/api/pg/tables");
  pgTables = r.error ? [] : r.tables;
  pgTablesError = r.error || null;
}
let pgTablesError = null;

// Search box + results for existing Postgres tables, filling `input` -
// laid out like the Oracle table search: nothing listed until you type,
// the picked table always shown ticked. `note` (optional) gets a warning
// while the input names an existing table.
function pgTablePicker(input, note) {
  const wrap = document.createElement("div");
  const search = document.createElement("input");
  search.autocomplete = "off";
  search.placeholder = `search ${(pgTables || []).length} existing Postgres tables…`;
  const list = document.createElement("div");
  list.className = "tablelist";
  list.style.cssText = "max-height: 180px; overflow-y: auto;";
  const group = "pgpick" + Math.random().toString(36).slice(2);
  const render = () => {
    const f = search.value.trim().toLowerCase();
    const cur = input.value.trim();
    const picked = (pgTables || []).includes(cur) ? [cur] : [];
    const shown = [...picked, ...(f ? pgTables.filter((t) => t.includes(f) && t !== cur) : [])];
    list.replaceChildren(...shown.map((t) => {
      const row = document.createElement("label");
      const rb = document.createElement("input");
      rb.type = "radio";
      rb.name = group;
      rb.checked = t === cur;
      rb.onchange = () => { input.value = t; input.dispatchEvent(new Event("input")); };
      row.append(rb, document.createTextNode(" " + t));
      return row;
    }));
  };
  const refreshNote = () => {
    if (note) note.textContent = (pgTables || []).includes(input.value.trim())
      ? "Existing table: column names come from each source's mapping (Mappings tab) and must already exist in it. A full sync replaces all its rows."
      : "";
  };
  // one listener per input, even when the picker is rebuilt
  if (input._pickerOff) input._pickerOff.abort();
  input._pickerOff = new AbortController();
  input.addEventListener("input", () => { render(); refreshNote(); }, { signal: input._pickerOff.signal });
  search.oninput = render;
  render();
  refreshNote();
  wrap.append(search, list);
  if (note) wrap.appendChild(note);
  return wrap;
}

async function onPgTableChange() {
  const name = $("col_pg_table").value.trim();
  pgCols = null;
  if (pgTables && pgTables.includes(name)) {
    const r = await api("/api/pg/table/" + encodeURIComponent(name) + "/columns");
    if ($("col_pg_table").value.trim() !== name) return;  // typed on meanwhile
    pgCols = r.error ? null : r.columns;
  }
  $("pg_cols_list").innerHTML = (pgCols || [])
    .map((c) => `<option value="${c.name}">${c.type}</option>`).join("");
  $("col_pg_existing").textContent = pgCols
    ? `Existing table (${pgCols.length} columns): pick the Postgres column for each Oracle column. A full sync replaces all rows in it, unless Upsert is ticked.`
    : "";
  fillPgColSelects();
  checkTargets();
}

// Existing table: each row's target becomes a pick-list of its Postgres
// columns. A column already named like the target is preselected.
function fillPgColSelects() {
  document.querySelectorAll("#col_grid tbody tr").forEach((tr) => {
    const input = tr.querySelector(".col-target");
    const sel = tr.querySelector(".col-pgcol");
    input.hidden = !!pgCols;
    sel.hidden = !pgCols;
    if (!pgCols) {
      if (!input.value) input.value = input.dataset.orig;  // back from an existing table
      return;
    }
    const cur = input.value.trim().toLowerCase();
    const match = pgCols.some((c) => c.name === cur);
    sel.innerHTML = '<option value="">(not mapped)</option>' +
      pgCols.map((c) => `<option value="${c.name}">${c.name}  ·  ${c.type}</option>`).join("");
    sel.value = match ? cur : "";
    input.value = sel.value;
  });
}
$("col_pg_table").oninput = onPgTableChange;

// Flags used columns whose target is taken twice (red) or, on an existing
// table, isn't one of its columns (amber - the sync would fail on it).
// Returns the duplicate count so Save can refuse.
function checkTargets() {
  const rows = [...document.querySelectorAll("#col_grid tbody tr")];
  const used = rows.filter((tr) => tr.querySelector(".col-use").checked);
  const name = (tr) => tr.querySelector(".col-target").value.trim().toLowerCase();
  const counts = {};
  used.forEach((tr) => { counts[name(tr)] = (counts[name(tr)] || 0) + 1; });
  const existing = pgCols && new Set(pgCols.map((c) => c.name));
  let dups = 0;
  rows.forEach((tr) => {
    const input = tr.querySelector(".col-target");
    const on = tr.querySelector(".col-use").checked;
    const dup = on && counts[name(tr)] > 1;
    const missing = on && existing && !existing.has(name(tr));
    [input, tr.querySelector(".col-pgcol")].forEach((el) => {
      el.classList.toggle("dup", dup);
      el.classList.toggle("missing", !dup && !!missing);
      el.title = dup ? "Another Oracle column already maps here"
        : missing ? (existing && !name(tr) ? "Pick a Postgres column" : "Not a column of the existing table") : "";
    });
    if (dup) dups++;
  });
  renderPgOnly(new Set(used.map(name)));
  return dups;
}

// Columns of the existing table no Oracle column fills: the sync leaves
// them NULL / default, which fails outright if NOT NULL with no default.
function renderPgOnly(mapped) {
  const box = $("col_pg_only");
  const rest = (pgCols || []).filter((c) => !mapped.has(c.name));
  if (!rest.length) { box.innerHTML = ""; return; }
  const blocking = rest.filter((c) => c.not_null && !c.has_default);
  box.innerHTML =
    `<h3>Postgres-only columns (${rest.length}) — not filled from Oracle</h3>` +
    (blocking.length ? `<p class="status err">${blocking.length} are NOT NULL with no default — the sync will fail unless an Oracle column maps to them or you add them to the sync.</p>` : "") +
    '<table class="grid"><thead><tr><th>Postgres column</th><th>Type</th><th>Gets</th><th></th></tr></thead><tbody>' +
    rest.map((c) => {
      const gets = c.has_default ? "its default" : c.not_null ? '<span class="mismatch">nothing — NOT NULL</span>' : "NULL";
      return `<tr><td>${c.name}</td><td>${c.type}</td><td>${gets}</td>` +
        `<td><button type="button" class="secondary" data-add="${c.name}" title="Fill it with a fixed value, or a dictionary / lookup on an Oracle column">Add to Sync</button></td></tr>`;
    }).join("") + "</tbody></table>";
}

// "Add to Sync": the column moves into the grid as a Postgres-only row
$("col_pg_only").onclick = (e) => {
  const b = e.target.closest("button[data-add]");
  if (!b) return;
  const tr = pgOnlyRow({ name: "", target: b.dataset.add });
  $("col_grid").querySelector("tbody").appendChild(tr);
  fillPgColSelects();
  checkTargets();
  tr.querySelector(".col-pgkey").focus();
};

$("mapping_refresh").onclick = loadMappingSummary;

$("col_save").onclick = async () => {
  if (!currentColTable) return;
  if (checkTargets()) return setStatus($("columns_status"), "Two columns map to the same target (marked red)", false);
  if (document.querySelector("#col_grid .col-pgcol.missing:not([hidden])"))
    return setStatus($("columns_status"), "Pick a Postgres column for every used column (marked amber)", false);
  const rows = [...$("col_grid").querySelectorAll("tbody tr")];
  const columns = rows.map((tr) => ({
    name: tr.querySelector(".col-target").dataset.name,
    target: tr.querySelector(".col-target").value,
    skip: !tr.querySelector(".col-use").checked,
    pg_type: tr.querySelector(".col-pgtype").value || null,
    if_null: tr.querySelector(".col-ifnull").value || null,
    dictionary: tr.querySelector(".col-dict").value || null,
    lookup_with: tr.querySelector(".col-lkwith").hidden ? null : tr.querySelector(".col-lkwith").value.split(","),
    pg_only: tr.classList.contains("col-pgonly"),
  }));
  const body = {
    pg_table: $("col_pg_table").value || null, columns,
    fast_load: $("col_fast_load").checked,
    upsert: $("col_upsert").checked,
  };
  const r = await api("/api/table/" + encodeURIComponent(currentColTable) + "/mapping", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.error) return setStatus($("columns_status"), r.error, false);
  setStatus($("columns_status"), "Saved", true);
  loadPgTables(true);
  loadMappingSummary();
  loadDictionaries();  // the Dictionaries column lists what the saved mapping uses
};

$("col_new").onclick = async () => {
  if (!currentColTable) return;
  const r = await api("/api/table/" + encodeURIComponent(currentColTable) + "/mapping/new", { method: "POST" });
  if (r.error) return setStatus($("columns_status"), r.error, false);
  await loadColumns(r.key);
  setStatus($("columns_status"), "New mapping: tick the columns it needs, name the Postgres table, then Save.");
};

$("col_delete").onclick = async () => {
  if (!currentColTable) return;
  const t = currentColTable;
  const extra = t.includes(":");
  const msg = extra
    ? `Delete mapping ${t}?`
    : `Delete the ${t} mapping (and any extra mappings on it) and remove it from the selected tables?`;
  if (!confirm(msg + "\n\nOnly the mapping is removed from Data Flow - the Postgres table is not touched.")) return;
  await fetch("/api/table/" + encodeURIComponent(t) + "/mapping", { method: "DELETE" });
  if (extra) return loadColumns(t.split(":")[0]);
  selection = selection.filter((x) => x !== t);
  renderTables();
  currentColTable = null;
  $("col_table_title").textContent = "Pick a table on the left";
  $("col_grid").querySelector("tbody").innerHTML = "";
  $("col_pg_table").value = "";
  ["col_pg_table", "col_fast_load", "col_upsert", "col_save", "col_new"].forEach((id) => { $(id).disabled = true; });
  $("col_delete").hidden = true;
  setStatus($("columns_status"), `Deleted ${t}`, true);
  loadMappingSummary();
};

// ---------- migrate / compare / sync ----------
// ---------- table groups ----------
let allGroups = [];
let groupDraft = null;
const groupCols = {};   // oracle table -> column names, fetched on demand

const blankGroup = () => ({ target: "", mode: "join", sources: [], joins: [], parts: [] });

async function loadGroups() {
  const r = await api("/api/groups");
  if (r.error) {
    $("group_list").innerHTML =
      `<p class="status err">${r.error}</p>` +
      '<p class="status">If this is a 404, the server is still running the ' +
      "older code — restart it to pick up the Groups API.</p>";
    allGroups = [];
    return;
  }
  allGroups = r.groups || [];
  renderGroupList();
  await loadPgTables(true);
  if (groupDraft) openGroup(groupDraft);
  if (!allTables.length) {
    const t = await api("/api/oracle/tables");
    if (!t.error) allTables = t.tables;
    if (groupDraft) renderGroupEditor();
  }
}

function renderGroupList() {
  const list = $("group_list");
  list.innerHTML = "";
  if (!allGroups.length) {
    list.innerHTML = '<p class="status">No groups yet. "New group" to combine two or more tables.</p>';
    return;
  }
  allGroups.forEach((g) => {
    const item = document.createElement("div");
    item.className = "mapping-item" + (groupDraft && groupDraft.target === g.target ? " active" : "");
    item.innerHTML =
      `<div class="mapping-item-name">${g.target}</div>` +
      `<div class="mapping-item-badges"><span class="badge">${g.mode}</span>` +
      `<span class="badge muted">${g.sources.join(" + ")}</span></div>`;
    item.onclick = () => openGroup(g);
    list.appendChild(item);
  });
}

function openGroup(g) {
  if (!g) return;
  groupDraft = JSON.parse(JSON.stringify(g));
  $("group_editor").hidden = false;
  $("group_title").textContent = g.target ? `Group: ${g.target}` : "New group";
  $("group_target").value = groupDraft.target;
  const note = document.createElement("p");
  note.className = "status warn";
  $("group_target_pick").replaceChildren(pgTablePicker($("group_target"), note));
  $("group_mode").value = groupDraft.mode;
  $("group_delete").hidden = !g.target;
  setStatus($("group_status"), "");
  $("group_errors").innerHTML = "";
  const draft = groupDraft;
  Promise.all(draft.sources.map(ensureColumns))
    .then(() => { if (groupDraft === draft) renderGroupEditor(); });
  renderGroupEditor();
  renderGroupList();
}

$("group_new").onclick = () => openGroup(blankGroup());
$("group_target").oninput = () => {
  if (groupDraft) groupDraft.target = $("group_target").value;
};
$("group_filter").oninput = () => { if (groupDraft) renderGroupEditor(); };
$("group_mode").onchange = () => {
  groupDraft.mode = $("group_mode").value;
  renderGroupEditor();
};

async function ensureColumns(table) {
  if (groupCols[table]) return groupCols[table];
  const r = await api(`/api/table/${encodeURIComponent(table)}/columns`);
  if (r.error || !r.columns) return [];  // don't cache a failure - retry next render
  groupCols[table] = r.columns.map((c) => c.name);
  return groupCols[table];
}

// A source can only link to one joined before it, so the editor offers
// exactly the tables earlier in the order — mirroring groups._validate_joins.
const earlierSources = (src) => groupDraft.sources.slice(0, groupDraft.sources.indexOf(src));

function linkFor(right) {
  let j = groupDraft.joins.find((x) => x.right === right);
  if (!j) {
    const earlier = earlierSources(right);
    j = { right, left: earlier[earlier.length - 1] || "", type: "inner", on: [{ left_col: "", right_col: "" }] };
    groupDraft.joins.push(j);
  }
  return j;
}

function renderGroupEditor() {
  const picker = $("group_sources");
  picker.innerHTML = "";
  const split = groupDraft.mode === "split";
  const f = $("group_filter").value.trim().toUpperCase();
  // nothing listed until a search is typed (schemas run to thousands of
  // tables); checked tables stay visible regardless of the search
  const pool = [...groupDraft.sources,
    ...(f ? allTables.filter((t) => t.includes(f) && !groupDraft.sources.includes(t)) : [])];
  $("group_sources_hint").textContent = !allTables.length
    ? "Couldn't load Oracle tables — check the connection."
    : split ? "Search and check the one table to split."
    : "Search and check to add or remove. Order matters: the first table drives the join.";
  $("group_target_label").textContent = split ? "Group name" : "Postgres table";
  $("group_target_pick").hidden = split;  // a split's name isn't a table
  pool.forEach((t) => {
    const row = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = groupDraft.sources.includes(t);
    cb.onchange = async () => {
      if (cb.checked) {
        if (split) groupDraft.sources = [];  // a split has exactly one source
        groupDraft.sources.push(t);
        await ensureColumns(t);
      } else {
        groupDraft.sources = groupDraft.sources.filter((x) => x !== t);
        groupDraft.joins = groupDraft.joins.filter((j) => j.right !== t && j.left !== t);
      }
      renderGroupEditor();
    };
    row.append(cb, document.createTextNode(" " + t));
    picker.appendChild(row);
  });

  const order = $("group_order");
  order.innerHTML = "";
  groupDraft.sources.forEach((t, i) => {
    const chip = document.createElement("span");
    chip.className = "group-chip";
    chip.innerHTML = `<span class="pos">${i === 0 ? "drives" : i + 1}</span> ${t}`;
    if (i > 0) {
      const up = document.createElement("button");
      up.className = "secondary";
      up.textContent = "↑";
      up.title = "move earlier";
      up.onclick = () => {
        const s = groupDraft.sources;
        [s[i - 1], s[i]] = [s[i], s[i - 1]];
        renderGroupEditor();
      };
      chip.appendChild(up);
    }
    order.appendChild(chip);
  });

  $("group_links_wrap").hidden = groupDraft.mode !== "join";
  if (groupDraft.mode === "join") renderGroupLinks();
  $("group_parts_wrap").hidden = !split;
  if (split) renderGroupParts();
}

function renderGroupParts() {
  groupDraft.parts = groupDraft.parts || [];
  const wrap = $("group_parts");
  wrap.innerHTML = "";
  const cols = groupCols[groupDraft.sources[0]] || [];
  groupDraft.parts.forEach((p, pi) => {
    const box = document.createElement("div");
    box.className = "group-link";
    const head = document.createElement("div");
    head.className = "row";
    const name = document.createElement("input");
    name.placeholder = "postgres_table";
    name.value = p.pg_table;
    name.autocomplete = "off";
    name.oninput = () => { p.pg_table = name.value; };
    const where = document.createElement("input");
    where.placeholder = "row filter, e.g. STATUS = 'A'";
    where.value = p.where || "";
    where.oninput = () => { p.where = where.value; };
    const rm = document.createElement("button");
    rm.className = "secondary";
    rm.textContent = "×";
    rm.onclick = () => { groupDraft.parts.splice(pi, 1); renderGroupParts(); };
    head.append(name, where, rm);
    box.appendChild(head);
    const note = document.createElement("p");
    note.className = "status warn";
    box.appendChild(pgTablePicker(name, note));

    const list = document.createElement("div");
    list.className = "tablelist";
    list.style.cssText = "max-height: 140px; overflow-y: auto;";
    if (!cols.length) list.innerHTML = '<p class="status">Pick the Oracle table first.</p>';
    cols.forEach((c) => {
      const lbl = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = p.columns.includes(c);
      cb.onchange = () => {
        p.columns = cb.checked ? [...p.columns, c] : p.columns.filter((x) => x !== c);
      };
      lbl.append(cb, document.createTextNode(" " + c));
      list.appendChild(lbl);
    });
    box.appendChild(list);
    wrap.appendChild(box);
  });
}

$("group_add_part").onclick = () => {
  groupDraft.parts = groupDraft.parts || [];
  groupDraft.parts.push({ pg_table: "", columns: [], where: "" });
  renderGroupParts();
};

function colSelect(table, value, onPick) {
  const sel = document.createElement("select");
  const fill = (cols) => {
    sel.innerHTML = '<option value="">(column)</option>' +
      cols.map((c) => `<option${c === value ? " selected" : ""}>${c}</option>`).join("");
  };
  fill(groupCols[table] || []);
  if (!groupCols[table] && table) ensureColumns(table).then(fill);
  sel.onchange = () => onPick(sel.value);
  return sel;
}

function renderGroupLinks() {
  const wrap = $("group_links");
  wrap.innerHTML = "";
  const joined = groupDraft.sources.slice(1);
  if (!joined.length) {
    wrap.innerHTML = '<p class="status">Add a second table to define a link.</p>';
    return;
  }
  joined.forEach((right) => {
    const j = linkFor(right);
    const box = document.createElement("div");
    box.className = "group-link";

    const head = document.createElement("div");
    head.className = "row";
    head.append(document.createTextNode(right + " joins to "));
    const leftSel = document.createElement("select");
    const earlier = earlierSources(right);
    leftSel.innerHTML = earlier
      .map((t) => `<option${t === j.left ? " selected" : ""}>${t}</option>`).join("");
    leftSel.onchange = () => { j.left = leftSel.value; renderGroupLinks(); };
    head.appendChild(leftSel);
    head.append(document.createTextNode(" as "));
    const typeSel = document.createElement("select");
    typeSel.innerHTML =
      `<option value="inner"${j.type === "inner" ? " selected" : ""}>inner (only matched rows)</option>` +
      `<option value="left"${j.type === "left" ? " selected" : ""}>left (keep unmatched, NULL-filled)</option>`;
    typeSel.onchange = () => { j.type = typeSel.value; };
    head.appendChild(typeSel);
    box.appendChild(head);

    j.on.forEach((pair, pi) => {
      const line = document.createElement("div");
      line.className = "group-pair";
      line.appendChild(colSelect(j.left, pair.left_col, (v) => { pair.left_col = v; }));
      line.append(document.createTextNode("="));
      line.appendChild(colSelect(right, pair.right_col, (v) => { pair.right_col = v; }));
      if (j.on.length > 1) {
        const rm = document.createElement("button");
        rm.className = "secondary";
        rm.textContent = "×";
        rm.onclick = () => { j.on.splice(pi, 1); renderGroupLinks(); };
        line.appendChild(rm);
      }
      box.appendChild(line);
    });

    const add = document.createElement("button");
    add.className = "secondary";
    add.textContent = "+ column pair";
    add.title = "for a composite key";
    add.onclick = () => { j.on.push({ left_col: "", right_col: "" }); renderGroupLinks(); };
    box.appendChild(add);
    wrap.appendChild(box);
  });
}

$("group_suggest").onclick = async () => {
  if (groupDraft.sources.length < 2) return setStatus($("group_status"), "Add two tables first", false);
  setStatus($("group_status"), "Looking for foreign keys…");
  const r = await api("/api/oracle/foreign-keys?tables=" + encodeURIComponent(groupDraft.sources.join(",")));
  if (r.error) return setStatus($("group_status"), r.error, false);
  let filled = 0;
  groupDraft.sources.slice(1).forEach((right) => {
    const earlier = earlierSources(right);
    // an FK either way round tells us how the two tables line up
    const fk = (r.fks || []).find(
      (f) => (f.table === right && earlier.includes(f.ref_table)) ||
             (f.ref_table === right && earlier.includes(f.table))
    );
    if (!fk) return;
    const j = linkFor(right);
    const rightIsChild = fk.table === right;
    j.left = rightIsChild ? fk.ref_table : fk.table;
    j.on = fk.columns.map((c, i) => rightIsChild
      ? { left_col: fk.ref_columns[i], right_col: c }
      : { left_col: c, right_col: fk.ref_columns[i] });
    filled++;
  });
  renderGroupLinks();
  setStatus($("group_status"), filled ? `Filled ${filled} link(s) from FKs` : "No foreign keys found between these tables", !!filled);
};

$("group_save").onclick = async () => {
  groupDraft.target = $("group_target").value;
  groupDraft.mode = $("group_mode").value;
  // only links for tables still in the group, in join order
  groupDraft.joins = groupDraft.sources.slice(1)
    .map((right) => groupDraft.joins.find((j) => j.right === right))
    .filter(Boolean);
  if (groupDraft.mode !== "join") groupDraft.joins = [];
  if (groupDraft.mode !== "split") groupDraft.parts = [];
  $("group_errors").innerHTML = "";
  setStatus($("group_status"), "Saving…");
  const r = await api("/api/groups", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(groupDraft),
  });
  if (r.errors) {
    setStatus($("group_status"), "Not saved", false);
    $("group_errors").innerHTML =
      r.errors.map((e) => `<p class="status err">${e}</p>`).join("");
    return;
  }
  if (!r.group) return setStatus($("group_status"), r.error || "Save failed", false);
  setStatus($("group_status"), "Saved", true);
  await loadGroups();
  openGroup(r.group);
};

$("group_delete").onclick = async () => {
  if (!groupDraft.target) return;
  await fetch(`/api/groups/${encodeURIComponent(groupDraft.target)}`, { method: "DELETE" });
  groupDraft = null;
  $("group_editor").hidden = true;
  $("group_title").textContent = "No group open";
  await loadGroups();
};

function logLine(s) {
  const log = $("log");
  log.textContent += s + "\n";
  log.scrollTop = log.scrollHeight;
}

$("create_tables").onclick = async () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Creating…");
  const r = await api("/api/create-tables", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: selection }),
  });
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  r.results.forEach((x) =>
    logLine(
      `[create] ${x.table} → ${x.pg_table}: ${x.status}` +
      (x.kind === "group" ? ` (${x.mode} of ${x.sources.join(", ")})` : "") +
      (x.error ? " — " + x.error : "")
    )
  );
  const errs = r.results.filter((x) => x.status === "error").length;
  setStatus($("migrate_status"), errs ? `${errs} errors (see log)` : "Tables ready", !errs);
  refreshDriftPanel();
};

// ---------- schema drift / recreate ----------
let driftTables = new Set();

function renderDriftPanel() {
  const panel = $("drift_panel");
  panel.innerHTML = "";
  driftTables.forEach((t) => {
    const div = document.createElement("div");
    div.className = "drift-item";
    const msg = document.createElement("span");
    msg.textContent = `${t}: mapping has changed since it was created. Recreate to apply changes — this drops and reloads all data.`;
    const btn = document.createElement("button");
    btn.className = "secondary";
    btn.textContent = `Recreate ${t}`;
    btn.onclick = () => recreateAndSync(t);
    div.append(msg, btn);
    panel.appendChild(div);
  });
}

async function refreshDriftPanel() {
  if (!selection.length) { driftTables = new Set(); renderDriftPanel(); return; }
  const r = await api("/api/mappings?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return;
  driftTables = new Set(r.rows.filter((x) => x.drift).map((x) => x.table));
  renderDriftPanel();
}

async function recreateAndSync(table) {
  logLine(`\n=== recreating ${table}: ${new Date().toLocaleTimeString()} ===`);
  const r = await api("/api/recreate-tables", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: [table] }),
  });
  if (r.error) return logLine(`FATAL: ${r.error}`);
  r.results.forEach((x) =>
    logLine(`[recreate] ${x.table} → ${x.pg_table}: ${x.status}${x.error ? " — " + x.error : ""}`)
  );
  // A merged target is rebuilt from every source at once, so the backend may
  // have recreated siblings we didn't ask for — re-sync all of them, or the
  // shared table keeps only this one source's rows.
  const group = [...new Set(r.results.map((x) => x.table))];
  group.forEach((t) => driftTables.delete(t));
  renderDriftPanel();
  runSync(group.length ? group : [table]);
}

function fmt(n) { return n === null || n === undefined ? "—" : n.toLocaleString(); }

let lastCompareRows = [];

function isMismatch(x) {
  return !x.error && x.pg !== null && !x.match;
}

function renderCompareRows(rows) {
  const tb = $("compare_table").querySelector("tbody");
  tb.innerHTML = "";
  rows.forEach((x) => {
    const tr = document.createElement("tr");
    let status = '<span class="pending">not created</span>';
    if (x.error) status = `<span class="mismatch">${x.error}</span>`;
    else if (x.pg !== null) status = x.match
      ? '<span class="match">match</span>'
      : '<span class="mismatch">mismatch</span>';
    tr.innerHTML =
      `<td>${x.table}</td><td>${x.pg_table}</td>` +
      `<td class="num">${fmt(x.oracle)}</td><td class="num">${fmt(x.pg)}</td>` +
      `<td class="num">${fmt(x.diff)}</td><td>${status}</td><td></td>`;
    const btn = document.createElement("button");
    btn.className = "secondary row-sync-btn";
    btn.textContent = "Sync";
    btn.onclick = () => runSync([x.table]);
    tr.lastElementChild.appendChild(btn);
    tb.appendChild(tr);
  });
}

async function refreshCompare() {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Counting…");
  const r = await api("/api/compare?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  lastCompareRows = r.rows;
  renderCompareRows(r.rows);
  setStatus($("migrate_status"), "Compared", true);
}

$("compare").onclick = refreshCompare;

function setSyncControlsDisabled(disabled) {
  $("sync_selected").disabled = disabled;
  $("sync_mismatched").disabled = disabled;
  document.querySelectorAll(".row-sync-btn").forEach((b) => { b.disabled = disabled; });
}

function fmtEta(sec) {
  if (sec === null || sec === undefined) return "";
  if (sec < 60) return `${Math.ceil(sec)}s`;
  const m = Math.floor(sec / 60), s = Math.ceil(sec % 60);
  return `${m}m ${s}s`;
}

function runSync(tables, freshTables) {
  setSyncControlsDisabled(true);
  setStatus($("migrate_status"), "Syncing…");
  logLine(`\n=== sync started: ${new Date().toLocaleTimeString()} ===`);
  let url = "/api/sync?tables=" + encodeURIComponent(tables.join(","));
  if (freshTables && freshTables.length) url += "&fresh=" + encodeURIComponent(freshTables.join(","));
  const es = new EventSource(url);
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "retry")
      logLine(`⟳ ${m.label}: ${m.error} — retrying in ${m.delay}s (attempt ${m.attempt}/${m.max_retries})…`);
    else if (m.type === "preflight_error")
      logLine(`✗ pre-flight [${m.check}${m.table ? " " + m.table : ""}]: ${m.error}`);
    else if (m.type === "resuming")
      logLine(`↻ ${m.table}: resuming from row ${fmt(m.offset)}`);
    else if (m.type === "table_start") logLine(`→ ${m.table}: loading…`);
    else if (m.type === "progress") {
      const rate = m.rows_per_sec ? `${fmt(Math.round(m.rows_per_sec))} rows/s` : "";
      const eta = m.eta_seconds != null ? `ETA ${fmtEta(m.eta_seconds)}` : "";
      const skipped = m.rows_skipped ? ` (${fmt(m.rows_skipped)} skipped)` : "";
      setStatus($("migrate_status"), `${m.table}: ${fmt(m.rows)} rows${skipped} — ${rate} ${eta}`.trim());
    }
    else if (m.type === "table_done") {
      const skipped = m.rows_skipped ? `, ${fmt(m.rows_skipped)} skipped (see History for detail)` : "";
      const c = m.changes;
      const changes = c ? ` — ${fmt(c.inserted)} new, ${fmt(c.updated)} updated, ${fmt(c.unchanged)} unchanged` : "";
      logLine(`✓ ${m.table}: ${fmt(m.rows_loaded)} rows loaded${changes}${skipped}  (oracle ${fmt(m.oracle)} / pg ${fmt(m.pg)}) ${m.match ? "MATCH" : "MISMATCH"}`);
      (m.dictionary_misses || []).forEach((x) => logLine(
        `⚠ ${m.table}.${x.column}: ${fmt(x.values.length)}${x.capped ? "+" : ""} value(s) not in dictionary ${x.dictionary} — those rows were skipped. ` +
        `Add them on the Dictionaries tab and sync again:\n    ${x.values.join(", ")}`));
      checkpointTables.delete(m.table);
      renderCheckpointPanel();
    }
    else if (m.type === "table_error") {
      logLine(`✗ ${m.table}: ${m.error}`);
      if (m.mapping_drift) { driftTables.add(m.table); renderDriftPanel(); }
    }
    else if (m.type === "constraints_dropped")
      logLine(`⚙ ${m.table}: dropped ${m.ok} constraint(s)/index(es) for fast sync${m.failed ? ` (${m.failed} failed to drop, see below)` : ""}`);
    else if (m.type === "constraints_restored")
      logLine(`⚙ ${m.table}: restored ${m.ok} constraint(s)/index(es)${m.failed ? ` — ${m.failed} FAILED, table may be under-constrained` : ""}`);
    else if (m.type === "constraint_error")
      logLine(`✗ ${m.table} [${m.kind}${m.name ? " " + m.name : ""}]: ${m.error}`);
    else if (m.type === "fatal") { logLine(`FATAL: ${m.error}`); es.close(); setSyncControlsDisabled(false); }
    else if (m.type === "all_done") {
      logLine("=== sync complete ===");
      es.close();
      setSyncControlsDisabled(false);
      setStatus($("migrate_status"), "Sync complete", true);
    }
  };
  es.onerror = () => { es.close(); setSyncControlsDisabled(false); setStatus($("migrate_status"), "Stream closed", false); };
}

$("sync_selected").onclick = () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  runSync(selection);
};

$("sync_mismatched").onclick = () => {
  const mismatched = lastCompareRows.filter(isMismatch).map((x) => x.table);
  if (!mismatched.length) return setStatus($("migrate_status"), "No mismatched tables — run Compare first", false);
  runSync(mismatched);
};

// ---------- resume checkpoints ----------
let checkpointTables = new Set();
let checkpointInfo = {};

function renderCheckpointPanel() {
  const panel = $("checkpoint_panel");
  panel.innerHTML = "";
  checkpointTables.forEach((t) => {
    const info = checkpointInfo[t] || {};
    const div = document.createElement("div");
    div.className = "checkpoint-item";
    const msg = document.createElement("span");
    msg.textContent = `${t}: interrupted sync — ${fmt(info.last_offset)} rows loaded so far. Syncing again resumes from there.`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const freshBtn = document.createElement("button");
    freshBtn.className = "secondary";
    freshBtn.textContent = "Start fresh";
    freshBtn.onclick = () => runSync([t], [t]);
    actions.appendChild(freshBtn);
    div.append(msg, actions);
    panel.appendChild(div);
  });
}

async function refreshCheckpointPanel() {
  if (!selection.length) { checkpointTables = new Set(); checkpointInfo = {}; renderCheckpointPanel(); return; }
  const r = await api("/api/checkpoints?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return;
  checkpointTables = new Set(r.rows.map((x) => x.table));
  checkpointInfo = Object.fromEntries(r.rows.map((x) => [x.table, x]));
  renderCheckpointPanel();
}

// ---------- dry run ----------
function renderDryRun(rows) {
  const panel = $("dryrun_panel");
  panel.innerHTML = "";
  rows.forEach((r) => {
    const card = document.createElement("div");
    card.className = "dryrun-card";
    if (r.error) {
      card.innerHTML = `<h4>${r.table}</h4><p class="status err">${r.error}</p>`;
      panel.appendChild(card);
      return;
    }
    const meta = document.createElement("div");
    meta.className = "dryrun-meta";
    meta.innerHTML =
      `<span>Oracle rows: ${fmt(r.oracle_rows)}</span>` +
      `<span>PG rows: ${r.exists ? fmt(r.pg_rows) : "table not created yet"}</span>` +
      (r.kind === "group" ? `<span>${r.mode} of ${r.sources.join(", ")}</span>` : "") +
      (r.drift ? `<span class="mismatch">mapping drift — recreate would change DDL</span>` : "") +
      (r.resumable_from != null ? `<span>resumable from row ${fmt(r.resumable_from)}</span>` : "");
    const heading = document.createElement("h4");
    heading.textContent = `${r.table} → ${r.pg_table}`;
    const pre = document.createElement("pre");
    pre.textContent = r.create_ddl;
    card.append(heading, meta, pre);
    if (r.join_sql) {
      const q = document.createElement("pre");
      q.textContent = `${r.join_sql}\nORDER BY ${r.order_by}`;
      card.appendChild(q);
    }
    if (r.renamed && r.renamed.length) {
      const note = document.createElement("div");
      note.className = "status warn";
      note.textContent = "Renamed to avoid collisions: " +
        r.renamed.map((x) => `${x.source}.${x.was} → ${x.now}`).join(", ");
      card.appendChild(note);
    }
    if (r.fast_load_drop) {
      const dropNote = document.createElement("div");
      const counts = Object.entries(r.fast_load_drop)
        .filter(([, v]) => v.length)
        .map(([k, v]) => `${v.length} ${k}`)
        .join(", ");
      dropNote.className = "status warn";
      dropNote.textContent = counts
        ? `Fast load would drop & recreate: ${counts}`
        : "Fast load enabled — no constraints/indexes currently found to drop";
      card.appendChild(dropNote);
    }
    panel.appendChild(card);
  });
}

$("dry_run").onclick = async () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Computing dry run…");
  const r = await api("/api/dry-run?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  renderDryRun(r.rows);
  setStatus($("migrate_status"), "Dry run ready — nothing was executed", true);
};

// ---------- sync history ----------
function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }
function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  return sec < 60 ? `${sec.toFixed(1)}s` : fmtEta(sec);
}

async function loadHistory() {
  setStatus($("history_status"), "Loading…");
  const r = await api("/api/sync-runs");
  const tb = $("history_table").querySelector("tbody");
  tb.innerHTML = "";
  (r.runs || []).forEach((run) => {
    const tr = document.createElement("tr");
    const statusClass = run.status === "success" ? "match" : run.status === "failed" ? "mismatch" : "pending";
    tr.innerHTML =
      `<td>${run.table}</td><td>${run.pg_table || "—"}</td>` +
      `<td>${fmtDate(run.started_at)}</td><td class="num">${fmtDuration(run.duration)}</td>` +
      `<td class="num">${fmt(run.rows_loaded)}</td><td class="num">${fmt(run.rows_skipped)}</td>` +
      `<td><span class="${statusClass}">${run.status}</span></td><td></td>`;
    const actions = tr.lastElementChild;
    if (run.rows_skipped) {
      const errBtn = document.createElement("button");
      errBtn.className = "secondary btn-sm";
      errBtn.textContent = "View errors";
      errBtn.onclick = () => toggleRunErrors(run.id, tr);
      actions.appendChild(errBtn);
    }
    const dl = document.createElement("a");
    dl.href = `/api/sync-runs/${run.id}/export`;
    dl.textContent = "Download";
    dl.className = "btn btn-sm";
    dl.style.marginLeft = "10px";
    actions.appendChild(dl);
    tb.appendChild(tr);
  });
  setStatus($("history_status"), `${(r.runs || []).length} runs`, true);
}

async function toggleRunErrors(runId, afterRow) {
  const existing = afterRow.nextElementSibling;
  if (existing && existing.classList.contains("errors-row")) {
    existing.remove();
    return;
  }
  const r = await api(`/api/sync-runs/${runId}/errors`);
  const tr = document.createElement("tr");
  tr.className = "errors-row";
  const td = document.createElement("td");
  td.colSpan = 8;
  const detail = document.createElement("div");
  detail.className = "errors-detail";
  detail.innerHTML = (r.errors || [])
    .map((e) => `<div>row ${e.row_ref}: ${e.error}</div>`)
    .join("") || "<div>No error detail found.</div>";
  td.appendChild(detail);
  tr.appendChild(td);
  afterRow.after(tr);
}

$("history_refresh").onclick = loadHistory;

// floating jump-to-top / jump-to-bottom buttons
$("scroll_top").onclick = () => window.scrollTo({ top: 0, behavior: "smooth" });
$("scroll_bottom").onclick = () => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });

// ---------- sets (menu at the top right) ----------
let allSets = [];
let activeSet = null;

const checkedValues = (box) => [...box.querySelectorAll("input:checked")].map((c) => c.value);
function checkItem(value, text, checked) {
  const label = document.createElement("label");
  const cb = Object.assign(document.createElement("input"), { type: "checkbox", value, checked });
  label.append(cb, document.createTextNode(text));
  return label;
}

async function loadSets() {
  const r = await api("/api/sets");
  if (r.error) return alert(r.error);
  allSets = r.sets;
  activeSet = r.sets.find((s) => s.id === r.active);
  $("set_name").textContent = activeSet.name;
  $("set_dot").style.background = $("set_btn").style.borderColor = activeSet.color;
  document.title = `${activeSet.name} · Data Flow`;
  $("set_edit_name").value = activeSet.name;
  $("set_edit_color").value = activeSet.color;
  $("set_delete").disabled = allSets.length < 2;
  $("dict_import_btn").hidden = allSets.length < 2;
  $("set_list").replaceChildren(...allSets.map((s) => {
    const b = document.createElement("button");
    const dot = Object.assign(document.createElement("span"), { className: "set-dot" });
    dot.style.background = s.color;
    b.append(dot, document.createTextNode(s.name));
    b.classList.toggle("active", s.id === activeSet.id);
    b.onclick = () => switchSet(s.id);
    return b;
  }));
}

// Every tab shows the active set, so a switch reloads the page - on the tab that was open.
function reloadHere() {
  location.hash = document.querySelector(".tab.active").dataset.tab;
  location.reload();
}

async function switchSet(id) {
  if (id === activeSet.id) return;
  const r = await api(`/api/sets/${id}/activate`, { method: "POST" });
  if (r.error) return alert(r.error);
  reloadHere();
}

const postJson = (url, body, method = "POST") =>
  api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// the menu sits under its button; it's in the top layer, so placed by hand
$("set_menu").addEventListener("beforetoggle", (e) => {
  if (e.newState !== "open") return;
  const b = $("set_btn").getBoundingClientRect();
  $("set_menu").style.top = `${b.bottom + 6}px`;
  $("set_menu").style.right = `${document.documentElement.clientWidth - b.right}px`;
});
$("set_menu").addEventListener("click", (e) => { if (e.target.closest("button")) $("set_menu").hidePopover(); });
document.querySelectorAll("#set_menu [data-same]").forEach((b) => { b.onclick = () => $(b.dataset.same).click(); });

$("set_new").onclick = async () => {
  const name = prompt("Name of the new set (it starts empty):");
  if (!name) return;
  const r = await postJson("/api/sets", { name });
  if (r.error) return alert(r.error);
  switchSet(r.result);
};

$("set_save").onclick = async () => {
  const r = await postJson(`/api/sets/${activeSet.id}`,
    { name: $("set_edit_name").value, color: $("set_edit_color").value }, "PUT");
  setStatus($("set_status"), r.error || "Saved", !r.error);
  if (!r.error) loadSets();
};

$("set_save_as").onclick = async () => {
  const name = prompt("Name of the new set. It gets this set's saved Oracle and Postgres connections; " +
                      "tables, groups, dictionaries and mappings start empty.", `${activeSet.name} copy`);
  if (!name) return;
  const r = await postJson("/api/sets", { name, copy_connections: true });
  if (r.error) return setStatus($("set_status"), r.error, false);
  switchSet(r.result);
};

$("set_delete").onclick = async () => {
  if (!confirm(`Delete the set ${activeSet.name} - its connections, tables, groups, dictionaries, mappings and sync history?\n\n` +
               "Oracle and Postgres are not touched. Export it first to keep a copy.")) return;
  const r = await api(`/api/sets/${activeSet.id}`, { method: "DELETE" });
  if (r.error) return setStatus($("set_status"), r.error, false);
  reloadHere();
};

// ---------- export ----------
function openExport(ids) {
  $("export_sets").replaceChildren(...allSets.map((s) => checkItem(s.id, s.name, ids.includes(s.id))));
  $("export_passwords").checked = false;
  setStatus($("export_status"), "");
  $("export_dlg").showModal();
}
$("set_export_one").onclick = () => openExport([activeSet.id]);
$("set_export_some").onclick = () => openExport([]);
$("set_export_all").onclick = () => openExport(allSets.map((s) => s.id));
$("export_cancel").onclick = () => $("export_dlg").close();
$("export_go").onclick = () => {
  const ids = checkedValues($("export_sets"));
  if (!ids.length) return setStatus($("export_status"), "Tick at least one set", false);
  Object.assign(document.createElement("a"), {
    href: `/api/export?sets=${ids}&passwords=${$("export_passwords").checked}`, download: "",
  }).click();
  $("export_dlg").close();
};

// ---------- import ----------
let importFile = null;
$("set_import").onclick = $("first_run_import").onclick = () => $("import_file").click();
$("import_file").onchange = async () => {
  const file = $("import_file").files[0];
  $("import_file").value = "";
  if (!file) return;
  const r = await api("/api/import?mode=inspect", { method: "POST", body: file });
  if (r.error) return alert(r.error);
  importFile = file;
  $("import_title").textContent = `Import ${file.name}`;
  $("import_sets").replaceChildren(...r.sets.map((s) =>
    checkItem(s.id, s.name + (s.exists ? " — you have a set with this name" : ""), true)));
  $("import_overwrite_row").hidden = !r.sets.some((s) => s.exists);
  $("import_overwrite").checked = false;
  setStatus($("import_status"), "");
  $("import_dlg").showModal();
};
$("import_cancel").onclick = () => $("import_dlg").close();

$("import_add").onclick = async () => {
  const ids = checkedValues($("import_sets"));
  if (!ids.length) return setStatus($("import_status"), "Tick at least one set", false);
  setStatus($("import_status"), "Importing…");
  const r = await api(`/api/import?mode=add&sets=${ids}&overwrite=${$("import_overwrite").checked}`,
    { method: "POST", body: importFile });
  if (r.error) return setStatus($("import_status"), r.error, false);
  alert(`Added ${r.added.join(", ")}. The previous data was saved as ${r.backup}.\n` +
        "Switch sets with the menu at the top right.");
  reloadHere();
};

$("import_replace").onclick = async () => {
  if (!confirm(`Replace ALL sets here - connections, mappings, groups, dictionaries and history - with the contents of ${importFile.name}?\n\n` +
               "A backup of the current data is kept next to the database file.")) return;
  setStatus($("import_status"), "Importing…");
  const r = await api("/api/import?mode=replace", { method: "POST", body: importFile });
  if (r.error) return setStatus($("import_status"), r.error, false);
  alert(`Imported. The previous data was saved as ${r.backup}.\nThe page will reload.`);
  reloadHere();
};

// ---------- dictionaries from another set (Dictionaries tab) ----------
async function loadDictImportList() {
  $("dict_import_all").checked = false;
  const r = await api(`/api/sets/${$("dict_import_set").value}/dictionaries`);
  if (r.error) return setStatus($("dict_import_status"), r.error, false);
  const mine = new Set(dictData.dictionaries.map((d) => d.name));
  $("dict_import_list").replaceChildren(...(r.dictionaries.length
    ? r.dictionaries.map((d) => checkItem(d.name,
        `${d.name} (${fmt(d.entries)} entries)${mine.has(d.name) ? " — replaces yours" : ""}`, false))
    : [Object.assign(document.createElement("p"), { className: "status", textContent: "That set has no dictionaries." })]));
}

$("dict_import_btn").onclick = () => {
  $("dict_import_set").replaceChildren(...allSets.filter((s) => s.id !== activeSet.id).map((s) => new Option(s.name, s.id)));
  setStatus($("dict_import_status"), "");
  $("dict_import").showModal();
  loadDictImportList();
};
$("dict_import_set").onchange = loadDictImportList;
$("dict_import_all").onchange = () =>
  $("dict_import_list").querySelectorAll("input").forEach((c) => { c.checked = $("dict_import_all").checked; });
$("dict_import_close").onclick = () => $("dict_import").close();

$("dict_import_go").onclick = async () => {
  const names = checkedValues($("dict_import_list"));
  if (!names.length) return setStatus($("dict_import_status"), "Tick at least one dictionary", false);
  const replacing = names.filter((n) => dictData.dictionaries.some((d) => d.name === n));
  if (replacing.length && !confirm(`Replace your ${replacing.join(", ")} with the other set's?`)) return;
  const r = await postJson("/api/dictionaries/import", { set_id: +$("dict_import_set").value, names });
  if (r.error) return setStatus($("dict_import_status"), r.error, false);
  setStatus($("dict_import_status"), `Imported ${names.join(", ")}`, true);
  await loadDictionaries();
  loadDictImportList();
};

// start on the tab a set switch / import reload came from
loadSets().then(loadConfig).then(() => {
  const tab = location.hash.slice(1);
  if (!tab) return;
  history.replaceState(null, "", location.pathname);
  document.querySelector(`.tab[data-tab="${CSS.escape(tab)}"]`)?.click();
});
