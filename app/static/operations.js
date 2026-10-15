/* All stored strings are rendered through textContent, never HTML interpolation. */
"use strict";
const $ = (id) => document.getElementById(id);
const state = { centers: [], units: [], panel: "centers", session: null, epoch: 0, busy: false, unitsReady: false };
const requests = { centers: 0, plans: 0, allocations: 0, weekly: 0 };
const base = window.FDE_API_BASE || "";
class StaleView extends Error {}
const snapshot = () => ({ actor: $("actor").value, epoch: state.epoch });
const isCurrent = scope => scope.actor === $("actor").value && scope.epoch === state.epoch;
function node(tag, text, attrs = {}) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}
function announce(message, kind = "ok") {
  $("notice").textContent = message; $("notice").dataset.kind = kind; $("notice").hidden = false;
  $("notice").setAttribute("role", kind === "error" ? "alert" : "status");
}
async function request(path, method = "GET", body) {
  const scope = snapshot();
  let response;
  try {
    response = await fetch(base + "/api/v1" + path, {
      method, headers: { "X-User": scope.actor, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (e) {
    if (!isCurrent(scope)) throw new StaleView();
    throw new Error("تعذر الاتصال بالخادم. تحقق من الاتصال ثم أعد المحاولة.");
  }
  if (!isCurrent(scope)) throw new StaleView();
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch (e) { /* gateway may return HTML */ }
    if (!isCurrent(scope)) throw new StaleView();
    const labels = { name: "اسم المركز", hourly_rate: "سعر الساعة", lines: "بنود التوزيع",
      estimated_hours: "الساعات التقديرية", week_start: "بداية الأسبوع", note: "ملاحظة الاعتماد" };
    const message = Array.isArray(data.detail)
      ? "راجع الحقول المطلوبة وقيمها: " + [...new Set(data.detail.map(x => labels[x.loc?.[1]] || "المدخلات"))].join("، ")
      : data.detail;
    throw new Error(message || `تعذر إكمال الطلب من الخادم (${response.status}). حاول مجدداً.`);
  }
  return response;
}
async function api(path, method = "GET", body) {
  const scope = snapshot();
  const response = await request(path, method, body);
  let data;
  try { data = await response.json(); }
  catch (e) { throw new Error("وصل رد غير صالح من الخادم. أعد المحاولة."); }
  if (!isCurrent(scope)) throw new StaleView();
  return data;
}
async function run(action) {
  try { await action(); } catch (e) {
    if (!(e instanceof StaleView)) announce(e.message, "error");
  }
}
async function mutate(trigger, action) {
  if (state.busy) return;
  state.busy = true;
  const text = trigger.textContent;
  trigger.disabled = true; trigger.textContent = "جار الحفظ…"; trigger.setAttribute("aria-busy", "true");
  $("actor").disabled = true;
  document.querySelectorAll("nav button").forEach(b => { b.disabled = true; });
  applyPermissions();
  try { await action(); }
  finally {
    state.busy = false; $("actor").disabled = false;
    document.querySelectorAll("nav button").forEach(b => { b.disabled = false; });
    if (trigger.isConnected) { trigger.disabled = false; trigger.textContent = text; trigger.removeAttribute("aria-busy"); }
    applyPermissions();
  }
}
function validateControls(...controls) {
  return controls.every(input => input.reportValidity());
}
function options(select, rows, label, selected = "") {
  select.replaceChildren();
  rows.forEach(row => select.add(new Option(label(row), String(row.id))));
  select.value = String(selected || rows[0]?.id || "");
}
function empty(tbody, columns, text) {
  const td = node("td", text, { colspan: columns, class: "empty" });
  tbody.replaceChildren(node("tr")); tbody.firstChild.append(td);
}
function cell(row, value) { const td = node("td"); td.append(value instanceof Node ? value : node("span", value ?? "—")); row.append(td); }
function button(text, action, mutation = false) {
  const el = node("button", text, { type: "button", class: "quiet" });
  el.addEventListener("click", () => run(() => mutation ? mutate(el, action) : action())); return el;
}
function hasRole(...roles) { return Boolean(state.session && roles.includes(state.session.role)); }
function canCenterEdit() { return hasRole("gm", "admin"); }
function canAllocate() { return hasRole("accountant", "finance"); }
function canApprove() { return hasRole("gm", "finance"); }
function canReadLanded() { return hasRole("gm", "finance", "accountant", "auditor", "engineer"); }
function canReadReport() { return hasRole("gm", "finance", "accountant", "auditor", "engineer"); }
function applyPermissions() {
  $("center-form").querySelectorAll("input,button").forEach(el => { el.disabled = !canCenterEdit() || state.busy; });
  $("allocation-form").querySelectorAll("input,select,button").forEach(el => { el.disabled = !canAllocate() || state.busy || !state.unitsReady; });
  const containers = state.units.filter(u => u.build_method === "CONTAINER");
  $("allocation-create").disabled = !canAllocate() || state.busy || !state.unitsReady ||
    !containers.length || !$("allocation-lines").children.length;
  $("add-allocation-line").disabled = !canAllocate() || state.busy || !state.unitsReady ||
    $("allocation-lines").children.length >= containers.length;
  syncBasis();
  $("weekly-form").querySelectorAll("input,select,button").forEach(el => { el.disabled = !canReadReport(); });
  window.FDEFinance?.permissions();
}
function clearWeekly(message = "اختر أسبوعاً لعرض التقرير.") {
  requests.weekly++;
  $("weekly-totals").replaceChildren(); $("weekly-body").replaceChildren();
  $("report-period").textContent = message;
  $("weekly-form").removeAttribute("aria-busy");
  applyPermissions();
}

async function loadUnits() {
  const selected = $("plan-unit").value;
  const selectedReport = $("report-unit").value;
  state.units = await api("/production-units");
  state.unitsReady = true;
  options($("plan-unit"), state.units, u => `${u.house_no} / ${u.build_method}`, selected);
  const report = $("report-unit"); report.replaceChildren(new Option("كل الوحدات", ""));
  state.units.forEach(u => report.add(new Option(u.house_no, String(u.id))));
  if (state.units.some(u => String(u.id) === selectedReport)) report.value = selectedReport;
  if (!state.units.length) $("plan-unit").add(new Option("لا توجد وحدات إنتاج", ""));
}
async function loadCenters() {
  const request = ++requests.centers;
  empty($("centers-body"), 5, "جار تحميل مراكز العمل…");
  const centers = await api("/work-centers");
  if (request !== requests.centers) return;
  state.centers = centers;
  const tbody = $("centers-body"); tbody.replaceChildren();
  applyPermissions();
  if (!state.centers.length) empty(tbody, 5, "لم تُضف مراكز عمل بعد. أضف المركز الأول أعلاه بصلاحية المدير العام.");
  state.centers.forEach(center => {
    const tr = node("tr"), name = node("input", undefined, { value: center.name, maxlength: 64, required: "", "aria-label": "اسم المركز" });
    const rate = node("input", undefined, { type: "number", min: 0, max: "99999999.99", step: ".01", required: "", value: center.hourly_rate, "aria-label": "السعر" });
    const active = node("select", undefined, { "aria-label": "حالة المركز" });
    active.add(new Option("نشط", "true")); active.add(new Option("معطل", "false")); active.value = String(center.is_active);
    const save = button("حفظ", async () => {
      if (!validateControls(name, rate)) return;
      if (!name.value.trim()) throw new Error("اسم مركز العمل مطلوب.");
      await api(`/work-centers/${center.id}`, "PATCH", { name: name.value, hourly_rate: rate.value,
        is_active: active.value === "true", version: center.version });
      announce("تم حفظ المركز. تقارير العمالة السابقة لم تتغير."); await loadCenters(); await loadPlans();
    }, true);
    for (const el of [name, rate, active, save]) el.disabled = !canCenterEdit();
    cell(tr, name); cell(tr, rate); cell(tr, active); cell(tr, center.version); cell(tr, save); tbody.append(tr);
  });
}
async function loadPlans() {
  const request = ++requests.plans;
  const tbody = $("plans-body"); tbody.replaceChildren();
  const unit = $("plan-unit").value;
  if (!unit) { empty(tbody, 5, "اختر وحدة لديها أوامر تصنيع لعرض مراحلها."); return; }
  empty(tbody, 5, "جار تحميل المراحل…");
  const rows = await api(`/production-units/${unit}/work-orders`);
  if (request !== requests.plans || $("plan-unit").value !== unit) return;
  tbody.replaceChildren();
  if (!rows.length) empty(tbody, 5, "هذه الوحدة لا تحتوي أوامر عمل بعد.");
  rows.forEach(wo => {
    const tr = node("tr"), select = node("select", undefined, { "aria-label": `مركز ${wo.operation}` });
    select.add(new Option("بدون مركز", ""));
    state.centers.filter(c => c.is_active || c.id === wo.work_center_id).forEach(c => {
      const option = new Option(c.name + (c.is_active ? "" : " / معطل"), String(c.id)); option.disabled = !c.is_active; select.add(option);
    });
    select.value = String(wo.work_center_id || "");
    const hours = node("input", undefined, { type: "number", min: 0, max: 100000, step: ".01", required: "", value: wo.estimated_hours, "aria-label": `ساعات ${wo.operation}` });
    const save = button("حفظ الخطة", async () => {
      if (!validateControls(hours)) return;
      await api(`/work-orders/${wo.id}/plan`, "PATCH", {
        work_center_id: select.value ? Number(select.value) : null, estimated_hours: Number(hours.value),
      }); announce("تم حفظ خطة المرحلة."); await loadPlans();
    }, true);
    const editable = hasRole("engineer", "admin") && wo.status === "PENDING";
    for (const el of [select, hours, save]) el.disabled = !editable;
    cell(tr, wo.operation); cell(tr, wo.status); cell(tr, select); cell(tr, hours); cell(tr, save); tbody.append(tr);
  });
}
function addLine() {
  const row = node("div", undefined, { class: "allocation-line" });
  const select = node("select", undefined, { "aria-label": "الحاوية المستفيدة" });
  const containers = state.units.filter(u => u.build_method === "CONTAINER");
  const used = new Set(Array.from($("allocation-lines").querySelectorAll("select"), el => el.value));
  const unused = containers.find(u => !used.has(String(u.id)));
  options(select, containers, u => u.house_no, unused?.id);
  if (!containers.length) select.add(new Option("لا توجد حاويات", ""));
  const value = node("input", undefined, { type: "number", min: ".0001", step: ".0001", required: "", value: "1", "aria-label": "قيمة أساس التوزيع" });
  const remove = button("حذف", () => { row.remove(); applyPermissions(); });
  select.required = true;
  row.append(select, value, remove); $("allocation-lines").append(row); applyPermissions();
}
function syncBasis() {
  document.querySelectorAll("#allocation-lines input").forEach(input => {
    const equal = $("allocation-basis").value === "EQUAL";
    input.disabled = equal || !canAllocate() || state.busy;
    if (equal) input.value = "1";
  });
}
async function loadAllocations() {
  const request = ++requests.allocations;
  applyPermissions();
  const list = $("allocations-list"); list.replaceChildren();
  if (!canReadLanded()) { list.append(node("p", "هذا الدور غير مخول بقراءة التوزيعات.", { class: "hint" })); return; }
  list.append(node("p", "جار تحميل التوزيعات…", { class: "hint" }));
  const rows = await api("/landed-costs");
  if (request !== requests.allocations) return;
  list.replaceChildren();
  if (!rows.length) list.append(node("div", "لا توجد توزيعات بعد. أنشئ مسودة بمرجع المستند ثم اعتمدها بمستخدم آخر.", { class: "surface empty" }));
  rows.forEach(cost => {
    const card = node("article", undefined, { class: "surface allocation-card" }), summary = node("div", undefined, { class: "summary" });
    summary.append(node("strong", cost.source_reference), node("span", `${cost.total.toFixed(2)} SAR`),
      node("span", cost.status === "APPROVED" ? "معتمد" : "مسودة", { class: `status ${cost.status === "APPROVED" ? "approved" : ""}` }));
    const basisLabel = { EQUAL: "بالتساوي", WEIGHT: "حسب الوزن", VALUE: "حسب القيمة" }[cost.basis] || cost.basis;
    const details = node("details"), title = node("summary", `تفاصيل التوزيع / ${basisLabel}`); details.append(title);
    const table = node("table"), head = node("tr");
    ["الحاوية", "الأساس", "الشحن", "الجمارك", "الإجمالي"].forEach(t => head.append(node("th", t))); table.append(head);
    cost.lines.forEach(line => {
      const tr = node("tr"); [line.house_no, line.basis_value, line.freight.toFixed(2), line.customs.toFixed(2), line.total.toFixed(2)].forEach(v => cell(tr, v)); table.append(tr);
    });
    const wrap = node("div", undefined, { class: "table-wrap" }); wrap.append(table); details.append(wrap);
    card.append(summary, node("p", cost.description, { class: "hint" }), details);
    if (cost.status === "DRAFT" && canApprove() && cost.created_by !== state.session.id) {
      const form = node("form", undefined, { class: "approval-form" }), note = node("input", undefined, { placeholder: "سبب الاعتماد بعد مراجعة المستند", "aria-label": "ملاحظة الاعتماد", minlength: 3, maxlength: 512, required: "" });
      const submit = node("button", "اعتماد التوزيع"); form.append(note, submit);
      form.addEventListener("submit", e => { e.preventDefault(); run(() => mutate(submit, async () => {
        if (note.value.trim().length < 3) throw new Error("أدخل ملاحظة اعتماد لا تقل عن ثلاثة أحرف.");
        await api(`/landed-costs/${cost.id}/approve`, "POST", { note: note.value });
        announce("تم الاعتماد وإضافة نصيب كل حاوية إلى تكلفتها."); await loadAllocations();
      })); }); card.append(form);
    } else if (cost.status === "DRAFT" && canApprove() && cost.created_by === state.session.id) {
      card.append(node("p", "أنت مُعدّ هذا التوزيع؛ يجب أن يعتمده مستخدم آخر مخول.", { class: "hint" }));
    }
    list.append(card);
  });
}
async function showPanel(name) {
  state.epoch++;
  Object.keys(requests).forEach(key => { requests[key]++; });
  state.panel = name;
  $("notice").hidden = true;
  applyPermissions();
  document.querySelectorAll("main>section").forEach(s => { s.hidden = s.id !== name; });
  document.querySelectorAll("nav button").forEach(b => {
    b.classList.toggle("active", b.dataset.panel === name);
    if (b.dataset.panel === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  $("page-title").textContent = { centers: "مراكز العمل", landed: "الشحن والجمارك", weekly: "التقارير الأسبوعية", finance: "التكلفة والإقفال" }[name];
  // Obtain identity from the authenticated backend, not a hard-coded demo username.
  if (!state.session) state.session = await api("/me");
  if (!state.unitsReady) await loadUnits();
  applyPermissions();
  if (name === "centers") { await loadCenters(); await loadPlans(); }
  if (name === "landed") {
    if (!$("allocation-lines").children.length && state.units.some(u => u.build_method === "CONTAINER")) addLine();
    await loadAllocations();
  }
  if (name === "weekly") {
    clearWeekly(canReadReport() ? "اختر أسبوعاً لعرض التقرير." : "هذا الدور غير مخول بعرض التقارير الأسبوعية.");
  }
  if (name === "finance") await window.FDEFinance?.load();
}
$("center-form").addEventListener("submit", e => { e.preventDefault(); run(() => mutate($("center-create"), async () => {
  if (!$("center-name").value.trim()) throw new Error("اسم مركز العمل مطلوب.");
  await api("/work-centers", "POST", { name: $("center-name").value, hourly_rate: $("center-rate").value });
  $("center-form").reset(); announce("تمت إضافة مركز العمل."); await loadCenters();
})); });
$("allocation-form").addEventListener("submit", e => { e.preventDefault(); run(() => mutate($("allocation-create"), async () => {
  const lines = Array.from($("allocation-lines").children).map(row => ({
    unit_id: Number(row.querySelector("select").value), basis_value: row.querySelector("input").value,
  }));
  if (!lines.length) throw new Error("أضف حاوية واحدة على الأقل.");
  if (new Set(lines.map(line => line.unit_id)).size !== lines.length) throw new Error("اختر حاوية مختلفة لكل بند؛ لا يمكن تكرار الحاوية.");
  if (!Number($("freight").value) && !Number($("customs").value)) throw new Error("إجمالي الشحن والجمارك يجب أن يكون أكبر من صفر.");
  await api("/landed-costs", "POST", { source_reference: $("source-reference").value,
    description: $("allocation-description").value, freight: $("freight").value,
    customs: $("customs").value, basis: $("allocation-basis").value, lines });
  announce("تم حفظ المسودة. يجب أن يعتمدها مستخدم آخر مخول."); await loadAllocations();
})); });
$("add-allocation-line").addEventListener("click", addLine);
$("allocation-basis").addEventListener("change", syncBasis);
$("refresh-centers").addEventListener("click", () => run(async () => { await showPanel("centers"); }));
$("refresh-allocations").addEventListener("click", () => run(loadAllocations));
$("plan-unit").addEventListener("change", () => run(loadPlans));
$("actor").addEventListener("change", () => {
  state.session = null;
  window.FDEFinance?.clear();
  $("centers-body").replaceChildren(); $("plans-body").replaceChildren(); $("allocations-list").replaceChildren();
  clearWeekly();
  run(() => showPanel(state.panel));
});
$("theme-toggle").addEventListener("click", () => { document.body.classList.toggle("dark"); $("theme-toggle").textContent = document.body.classList.contains("dark") ? "الوضع الفاتح" : "الوضع الداكن"; });
document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => run(() => showPanel(b.dataset.panel))));
function weekQuery() {
  const p = new URLSearchParams({ week_start: $("week-start").value });
  if ($("report-unit").value) p.set("unit_id", $("report-unit").value);
  return p.toString();
}
function validWeek() {
  if (!$("weekly-form").reportValidity()) return false;
  const day = new Date(`${$("week-start").value}T00:00:00Z`);
  if (day.getUTCDay() !== 1) {
    announce("بداية التقرير الأسبوعي يجب أن تكون يوم الاثنين.", "error");
    $("week-start").focus(); return false;
  }
  return true;
}
function renderWeekly(report) {
  $("report-period").textContent = `${report.week_start} إلى ${report.week_end} / ${report.timezone}`;
  if (report.totals.zero_or_unconfigured_rate_hours > 0) {
    $("report-period").textContent += ` • تنبيه: ${report.totals.zero_or_unconfigured_rate_hours} ساعة بسعر صفر أو غير مضبوط.`;
  }
  const metrics = $("weekly-totals"); metrics.replaceChildren();
  for (const [label, value] of [["ساعات الأشخاص", report.totals.person_hours],
    ["تكلفة العمالة / SAR", report.totals.labor_cost], ["ساعات التوقف", report.totals.stoppage_hours]]) {
    const box = node("div", undefined, { class: "metric" });
    box.append(node("span", label), node("strong", value)); metrics.append(box);
  }
  const tbody = $("weekly-body"); tbody.replaceChildren();
  if (!report.units.length) empty(tbody, 7, "لا توجد وحدات ضمن نطاق التقرير.");
  report.units.forEach(unit => {
    const tr = node("tr"); [unit.house_no, `${unit.start_percent}% / ${unit.end_percent}%`,
      `${unit.progress_delta_points} نقطة`, unit.person_hours, unit.labor_cost,
      unit.stoppage_hours, unit.shortages.length].forEach(v => cell(tr, v)); tbody.append(tr);
  });
}
$("weekly-form").addEventListener("submit", e => { e.preventDefault(); run(async () => {
  if (!validWeek()) return;
  clearWeekly("جار إعداد التقرير…");
  const request = ++requests.weekly;
  const query = weekQuery(), trigger = $("weekly-form").querySelector("button");
  trigger.disabled = true; $("weekly-form").setAttribute("aria-busy", "true");
  try {
    const report = await api(`/reports/weekly?${query}`);
    if (request !== requests.weekly || query !== weekQuery()) return;
    renderWeekly(report); announce("تم إعداد التقرير من أحداث الفترة المحددة.");
  } catch (error) {
    if (request === requests.weekly && query === weekQuery()) throw error;
  } finally {
    if (request === requests.weekly) {
      $("weekly-form").removeAttribute("aria-busy"); applyPermissions();
    }
  }
}); });
$("weekly-csv").addEventListener("click", () => run(async () => {
  if (!validWeek()) return;
  const scope = snapshot(), query = weekQuery(), date = $("week-start").value;
  const trigger = $("weekly-csv"); trigger.disabled = true;
  try {
    const response = await request(`/reports/weekly.csv?${query}`);
    const blob = await response.blob();
    if (!isCurrent(scope) || query !== weekQuery()) throw new StaleView();
    const url = URL.createObjectURL(blob);
    const a = node("a", "", { href: url, download: `fde-weekly-${date}.csv` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (isCurrent(scope) && query === weekQuery()) throw error;
  } finally { if (isCurrent(scope)) applyPermissions(); }
}));
for (const id of ["week-start", "report-unit"]) {
  $(id).addEventListener(id === "week-start" ? "input" : "change", () => {
    $("notice").hidden = true;
    clearWeekly("تغيرت الفلاتر. اضغط «عرض التقرير» لتحديث النتائج.");
  });
}
const today = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Riyadh" }));
today.setDate(today.getDate() - (today.getDay() + 6) % 7);
$("week-start").value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
applyPermissions();
run(() => showPanel("centers"));
