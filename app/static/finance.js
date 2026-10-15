/* Uses the shared authenticated request, stale-view and mutation protections. */
"use strict";
window.FDEFinance = (() => {
  let check = null, generation = 0, invoiceCount = 0;
  const statuses = { DRAFT: "مسودة", APPROVED: "معتمد", CANCELLED: "ملغى", CLOSED: "مقفل", REOPENED: "أعيد فتحه" };
  function permissions() {
    const ready = canReadReport() && state.unitsReady && !state.busy;
    $("settlement-form").querySelectorAll("select,button").forEach(e => { e.disabled = !ready || !canAllocate() || !invoiceCount; });
    $("expense-form").querySelectorAll("input,select,button").forEach(e => { e.disabled = !ready || !canAllocate() || !state.units.length; });
    $("close-check-form").querySelectorAll("select,button").forEach(e => { e.disabled = !ready || !state.units.length; });
    $("close-note").disabled = !ready || !canApprove();
    $("financial-close-button").disabled = !ready || !canApprove() || !check?.can_close;
    $("financial-reopen-button").disabled = !ready || !canApprove() || !check?.current_close ||
      check.current_close.closed_by === state.session?.id;
  }
  function invalidate() {
    generation++; check = null;
    $("close-results").replaceChildren(); $("close-history").replaceChildren();
    $("close-action-form").hidden = true; $("close-note").value = "";
    permissions();
  }
  function clear() {
    invalidate(); invoiceCount = 0;
    for (const id of ["settlements-list", "expenses-list", "settlement-invoice", "expense-unit", "close-unit"])
      $(id).replaceChildren();
    permissions();
  }
  function card(title, details) {
    const el = node("article", undefined, { class: "surface" });
    el.append(node("h3", title), node("p", details)); return el;
  }
  function decisionForm(container, rec, path, callback, cancel = false) {
    if (rec.status !== "DRAFT") return;
    const allowed = cancel ? canAllocate() && state.session.id === rec.created_by
      : canApprove() && state.session.id !== rec.created_by;
    if (!allowed) {
      if (!cancel && state.session.id === rec.created_by) container.append(node("p", "الاعتماد لمستخدم آخر مخول.", { class: "hint" }));
      return;
    }
    const form = node("form", undefined, { class: "inline-form" });
    const note = node("input", undefined, { minlength: 3, maxlength: 512, required: "", placeholder: cancel ? "سبب إلغاء المسودة" : "ملاحظة الاعتماد", "aria-label": cancel ? "سبب إلغاء المصروف" : "ملاحظة الاعتماد" });
    const submit = node("button", cancel ? "إلغاء المسودة" : "اعتماد", { class: cancel ? "quiet" : "" });
    form.append(note, submit);
    form.addEventListener("submit", event => {
      event.preventDefault();
      run(() => mutate(submit, async () => {
        if (!form.reportValidity()) return;
        await api(path, "POST", { note: note.value });
        announce(cancel ? "ألغيت المسودة دون حذف سجلها." : "تم الاعتماد وتحديث التكلفة.");
        await callback();
      }));
    }); container.append(form);
  }
  async function load() {
    const selected = $("close-unit").value, selectedExpense = $("expense-unit").value;
    clear();
    if (!canReadReport()) {
      $("close-results").append(node("p", "هذا الدور غير مخول بعرض البيانات المالية.", { class: "surface" })); return;
    }
    const token = generation;
    $("settlements-list").append(node("p", "جار تحميل التسويات…"));
    const invoices = await api("/material-settlements/candidates");
    const settlements = await api("/material-settlements");
    const expenses = await api("/unit-expenses");
    if (token !== generation) return;
    invoiceCount = invoices.length;
    options($("settlement-invoice"), invoices, i => `${i.invoice_no} / PO ${i.purchase_order_id}`);
    if (!invoices.length) $("settlement-invoice").add(new Option("لا توجد فاتورة مؤهلة غير مسواة", ""));
    options($("expense-unit"), state.units, u => `${u.house_no} / ${u.build_method}`, selectedExpense);
    options($("close-unit"), state.units, u => `${u.house_no} / ${u.status}`, selected);
    $("settlements-list").replaceChildren();
    if (!settlements.length) $("settlements-list").append(node("p", "لا توجد تسويات أسعار مسجلة.", { class: "hint" }));
    for (const rec of settlements) {
      const el = card(`تسوية ${rec.id} / فاتورة ${rec.invoice_id} / ${statuses[rec.status]}`,
        `PO ${rec.purchase_order_id} • فرق أسعار كامل البنود: ${(rec.variance_cents / 100).toFixed(2)} SAR. يُوزع على المصروف والمخزون، وليس كله على وحدة واحدة.`);
      decisionForm(el, rec, `/material-settlements/${rec.id}/approve`, load);
      $("settlements-list").append(el);
    }
    $("expenses-list").replaceChildren();
    if (!expenses.length) $("expenses-list").append(node("p", "لا توجد مصاريف مسجلة.", { class: "hint" }));
    for (const rec of expenses) {
      const unit = state.units.find(u => u.id === rec.unit_id);
      const el = card(`${rec.source_reference} / ${statuses[rec.status]}`,
        `${unit?.house_no || rec.unit_id} • ${rec.category === "EQUIPMENT" ? "معدات" : "مصروف مباشر"} • ${rec.total.toFixed(2)} SAR • ${rec.description}`);
      decisionForm(el, rec, `/unit-expenses/${rec.id}/approve`, load);
      decisionForm(el, rec, `/unit-expenses/${rec.id}/cancel`, load, true);
      $("expenses-list").append(el);
    }
    permissions();
  }
  async function checkUnit() {
    invalidate(); const token = generation, uid = $("close-unit").value;
    if (!uid) return;
    $("close-results").append(node("p", "جار فحص التكلفة وموانع الإقفال…"));
    const result = await api(`/production-units/${uid}/financial-close/check`);
    const history = await api(`/production-units/${uid}/financial-close`);
    if (token !== generation || uid !== $("close-unit").value) return;
    check = result; $("close-results").replaceChildren();
    const sum = result.summary, box = card(
      result.current_close ? `مقفل مالياً / إصدار ${result.current_close.revision}` : result.can_close ? "جاهز للإقفال المالي" : "لم يستوف شروط الإقفال",
      sum.valuation === "settled_invoice_price" ? "المواد مسواة بفواتير معتمدة." : "تنبيه: المواد غير مسواة بالكامل، أو لم تسجل مواد مصروفة.");
    const table = node("table"), body = node("tbody");
    for (const [label, value] of [
      ["المواد", sum.actual.materials], ["العمالة", sum.actual.labor],
      ["غير المباشرة", sum.actual.overhead], ["الشحن والجمارك", sum.actual.landed_cost],
      ["مصاريف مباشرة", sum.actual.direct_expenses], ["معدات", sum.actual.equipment],
      ["إجمالي التكلفة / SAR", sum.actual.total],
    ]) { const tr = node("tr"); cell(tr, label); cell(tr, value === null ? "غير متاح" : Number(value).toFixed(2)); body.append(tr); }
    table.append(body); box.append(table);
    const blockers = node("ul"); result.blockers.forEach(b => blockers.append(node("li", b))); box.append(blockers);
    $("close-results").append(box);
    $("close-action-form").hidden = !canApprove();
    $("financial-close-button").hidden = Boolean(result.current_close);
    $("financial-reopen-button").hidden = !result.current_close;
    for (const record of history) {
      const details = node("details", undefined, { class: "surface" });
      details.append(node("summary", `إصدار ${record.revision} / ${statuses[record.status]} / ${record.snapshot.actual.total.toFixed(2)} SAR`));
      details.append(node("p", `بصمة SHA-256: ${record.snapshot_hash}`, { class: "finance-hash" }));
      details.append(node("p", record.note));
      if (record.reopen_note) details.append(node("p", `سبب إعادة الفتح: ${record.reopen_note}`));
      const pre = node("pre", JSON.stringify(record.snapshot, null, 2), { class: "finance-snapshot", dir: "ltr" });
      details.append(pre); $("close-history").append(details);
    }
    permissions();
  }
  $("settlement-form").addEventListener("submit", event => { event.preventDefault(); run(() => mutate($("settlement-create"), async () => {
    if (!$("settlement-form").reportValidity()) return;
    await api("/material-settlements", "POST", { invoice_id: Number($("settlement-invoice").value) });
    announce("أعدت مسودة تسوية؛ لا تغير التكلفة حتى اعتماد مستخدم آخر."); await load();
  })); });
  $("expense-form").addEventListener("submit", event => { event.preventDefault(); run(() => mutate($("expense-create"), async () => {
    if (!$("expense-form").reportValidity()) return;
    await api("/unit-expenses", "POST", {
      unit_id: Number($("expense-unit").value), source_reference: $("expense-reference").value,
      category: $("expense-category").value, description: $("expense-description").value,
      incurred_date: $("expense-date").value, net_amount: $("expense-net").value,
      nonrecoverable_tax: $("expense-tax").value,
    });
    $("expense-reference").value = ""; $("expense-net").value = ""; $("expense-description").value = ""; $("expense-tax").value = "0";
    announce("حفظت المسودة؛ لا تدخل التكلفة قبل الاعتماد."); await load();
  })); });
  $("close-check-form").addEventListener("submit", event => { event.preventDefault(); run(checkUnit); });
  $("close-unit").addEventListener("change", invalidate);
  $("finance-refresh").addEventListener("click", () => run(load));
  $("close-action-form").addEventListener("submit", event => { event.preventDefault(); run(() => mutate($("financial-close-button"), async () => {
    if (!check?.can_close || !$("close-action-form").reportValidity()) return;
    if (!window.confirm(`إقفال الوحدة بتكلفة ${check.summary.actual.total.toFixed(2)} SAR وتثبيت لقطة المراجعة؟`)) return;
    await api(`/production-units/${$("close-unit").value}/financial-close`, "POST", {
      note: $("close-note").value, expected_summary_hash: check.summary_hash,
    });
    announce("أقفلت الوحدة مالياً وحفظت لقطة التكلفة."); await checkUnit();
  })); });
  $("financial-reopen-button").addEventListener("click", () => run(() => mutate($("financial-reopen-button"), async () => {
    if (!check?.current_close || !$("close-action-form").reportValidity()) return;
    if (!window.confirm("إعادة فتح الوحدة للتعديل مع إبقاء لقطة الإقفال السابقة في سجل التدقيق؟")) return;
    await api(`/production-units/${$("close-unit").value}/financial-close/reopen`, "POST", {
      note: $("close-note").value, close_id: check.current_close.id,
    });
    announce("أعيد فتح الوحدة. يلزم فحص وإقفال جديد بعد التعديل."); await checkUnit();
  })));
  $("expense-date").value = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Riyadh" });
  permissions();
  return { load, clear, permissions };
})();
