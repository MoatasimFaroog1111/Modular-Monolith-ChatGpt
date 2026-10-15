/**
 * Deterministic UI regression tests against the real HTML/CSS/JS.
 * API responses are mocked, including delays and failures; no financial records
 * or user data are modified. Live end-to-end checks are documented separately.
 *
 * FDE_QA_URL=http://127.0.0.1:8765/operations npm test
 */
const { default: assert } = await import("node:assert/strict");
const { pathToFileURL } = await import("node:url");

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const identities = {
  gm1: { id: 3, username: "gm1", full_name: "المدير العام", role: "gm" },
  eng1: { id: 1, username: "eng1", full_name: "المهندس", role: "engineer" },
  acc1: { id: 5, username: "acc1", full_name: "المحاسب", role: "accountant" },
  fin1: { id: 6, username: "fin1", full_name: "المدير المالي", role: "finance" },
  aud1: { id: 7, username: "aud1", full_name: "المدقق", role: "auditor" },
  wh1: { id: 4, username: "wh1", full_name: "المستودع", role: "warehouse" },
};

async function fixture(browser, url, options = {}) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const calls = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const data = {
    centers: [{ id: 1, name: "مركز الاختبار", hourly_rate: 35, is_active: true, version: 1 }],
    units: options.emptyUnits ? [] : [
      { id: 1, house_no: "C-01", build_method: "CONTAINER" },
      { id: 2, house_no: "C-02", build_method: "CONTAINER" },
    ],
    workOrders: [{ id: 11, operation: "العزل", status: "PENDING", work_center_id: 1,
      estimated_hours: 5, actual_hours: 0 }],
    costs: [{ id: 1, source_reference: "SELF-FIN", description: "اختبار فصل الصلاحيات",
      total: 10, basis: "EQUAL", status: "DRAFT", created_by: 6,
      lines: [{ unit_id: 1, house_no: "C-01", basis_value: "1", freight: 10, customs: 0, total: 10 }] }],
  };
  await page.route("**/api/v1/**", async route => {
    const req = route.request(), parsed = new URL(req.url()), path = parsed.pathname.replace("/api/v1", "");
    const method = req.method(), user = identities[req.headers()["x-user"]] || identities.gm1;
    const body = req.postDataJSON();
    calls.push({ path, method, user: user.username, body, query: parsed.search });
    const json = (payload, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
    if (path === "/me") return json(user);
    if (path === "/production-units") return json(data.units);
    if (/\/production-units\/\d+\/work-orders/.test(path)) return json(data.workOrders);
    if (/\/work-orders\/\d+\/plan/.test(path)) {
      Object.assign(data.workOrders[0], body); return json({ id: 11, ...body });
    }
    if (path === "/work-centers" && method === "GET") {
      if (options.networkFailure) return route.abort("failed");
      return json(data.centers);
    }
    if (path === "/work-centers" && method === "POST") {
      await pause(options.mutationDelay || 0);
      const center = { id: data.centers.length + 1, ...body, hourly_rate: Number(body.hourly_rate), is_active: true, version: 1 };
      data.centers.push(center); return json(center);
    }
    if (/\/work-centers\/\d+/.test(path) && method === "PATCH") {
      if (options.conflict) return json({ detail: "تم تعديل المركز في جلسة أخرى؛ حدّث القائمة أولاً" }, 409);
      Object.assign(data.centers[0], body, { version: body.version + 1 }); return json(data.centers[0]);
    }
    if (path === "/landed-costs" && method === "GET") {
      await pause(options.allocationDelay || 0); return json(data.costs);
    }
    if (path === "/landed-costs" && method === "POST") {
      await pause(options.mutationDelay || 0);
      const cost = { id: data.costs.length + 1, ...body, total: Number(body.freight) + Number(body.customs),
        created_by: user.id, status: "DRAFT", lines: body.lines.map(ln => ({
          ...ln, house_no: `C-0${ln.unit_id}`, freight: 1, customs: 0, total: 1,
        })) };
      data.costs.unshift(cost); return json(cost, 201);
    }
    if (/\/landed-costs\/\d+\/approve/.test(path)) {
      await pause(options.mutationDelay || 0);
      data.costs[0].status = "APPROVED"; return json(data.costs[0]);
    }
    if (path === "/reports/weekly") {
      await pause(options.weeklyDelay || 0);
      if (options.reportFailure) return route.fulfill({ status: 503, contentType: "text/html", body: "<h1>gateway unavailable</h1>" });
      const units = data.units.filter(u => !parsed.searchParams.has("unit_id") || String(u.id) === parsed.searchParams.get("unit_id"))
        .map(u => ({ ...u, start_percent: 0, end_percent: 4, progress_delta_points: 4, person_hours: 28,
          labor_cost: 980, stoppage_hours: 2, shortages: [] }));
      return json({ week_start: "2026-10-12", week_end: "2026-10-18", timezone: "Asia/Riyadh",
        totals: { person_hours: units.length * 28, labor_cost: units.length * 980,
          stoppage_hours: units.length * 2, zero_or_unconfigured_rate_hours: 0 }, units });
    }
    if (path === "/reports/weekly.csv") return route.fulfill({
      status: 200, contentType: "text/csv; charset=utf-8", body: "\ufeffunit,hours\r\nC-01,28\r\n",
    });
    return json({ detail: "unmocked endpoint" }, 404);
  });
  await page.goto(url);
  if (options.networkFailure) await page.locator("#notice[data-kind=error]").waitFor();
  else await page.locator("#centers-body input").first().waitFor();
  return { page, calls, errors, data, options };
}

export async function runOperationsQA(browser, url = "http://127.0.0.1:8765/operations") {
  const results = [];
  async function test(name, fn, options) {
    let f;
    try {
      f = await fixture(browser, url, options);
      await fn(f);
      assert.deepEqual(f.errors, [], "No JavaScript exceptions");
      results.push({ name, passed: true });
    } catch (error) {
      results.push({ name, passed: false, error: error.message });
    } finally { if (f) await f.page.close(); }
  }
  const role = async (p, value) => {
    await p.locator("#actor").selectOption(value);
    await p.waitForResponse(r => r.url().endsWith("/api/v1/me"));
    await p.waitForTimeout(70);
  };
  const tab = async (p, name) => {
    await p.locator(`nav [data-panel=${name}]`).click();
    await p.locator(`#${name}`).waitFor();
  };
  const report = async p => {
    await tab(p, "weekly"); await p.locator("#week-start").fill("2026-10-12");
    await p.locator("#weekly-form button").first().click();
    await p.locator("#weekly-body tr").first().waitFor();
  };
  await test("blank and negative plan hours never submit", async ({ page, calls }) => {
    await role(page, "eng1");
    const row = page.locator("#plans-body tr").first();
    for (const value of ["", "-1"]) {
      await row.locator("input").fill(value);
      await row.getByRole("button", { name: "حفظ الخطة" }).click();
      await page.waitForTimeout(80);
    }
    assert.equal(calls.filter(c => c.method === "PATCH").length, 0);
    assert.equal(await row.locator("input").evaluate(el => el.validity.valid), false);
  });
  await test("self approval hidden; other approver can approve", async ({ page }) => {
    await role(page, "fin1"); await tab(page, "landed");
    await page.locator(".allocation-card").first().waitFor();
    assert.equal(await page.locator(".approval-form").count(), 0);
    await page.getByText("أنت مُعدّ هذا التوزيع؛ يجب أن يعتمده مستخدم آخر مخول.").waitFor();
    await role(page, "gm1"); await page.locator(".approval-form").waitFor();
    assert.equal(await page.locator(".approval-form").count(), 1);
  });
  await test("filters immediately invalidate old report", async ({ page }) => {
    await report(page); assert.equal(await page.locator("#weekly-body tr").count(), 2);
    await page.locator("#report-unit").selectOption("1");
    assert.equal(await page.locator("#weekly-body tr").count(), 0);
    assert.match(await page.locator("#report-period").innerText(), /تغيرت الفلاتر/);
    await page.locator("#weekly-form button").first().click();
    await page.locator("#weekly-body tr").first().waitFor();
    assert.equal(await page.locator("#weekly-body tr").count(), 1);
  });
  await test("typing a different date clears results before leaving the field", async ({ page }) => {
    await report(page);
    await page.locator("#week-start").fill("2026-10-19");
    assert.equal(await page.locator("#weekly-body tr").count(), 0);
  });
  await test("late error for old filters does not overwrite current view", async ({ page }) => {
    await tab(page, "weekly"); await page.locator("#week-start").fill("2026-10-12");
    const sent = page.waitForRequest(r => r.url().includes("/reports/weekly?"));
    await page.locator("#weekly-form button").first().click(); await sent;
    await page.locator("#report-unit").selectOption("1");
    await page.waitForTimeout(450);
    assert.equal(await page.locator("#notice").isHidden(), true);
  }, { weeklyDelay: 300, reportFailure: true });
  await test("late report cannot repopulate after role switch", async ({ page }) => {
    await tab(page, "weekly"); await page.locator("#week-start").fill("2026-10-12");
    const sent = page.waitForRequest(r => r.url().includes("/reports/weekly?"));
    await page.locator("#weekly-form button").first().click(); await sent;
    await role(page, "wh1"); await page.waitForTimeout(500);
    assert.equal(await page.locator("#weekly-body tr").count(), 0);
    assert.equal(await page.locator("#weekly-totals").innerText(), "");
    assert.equal(await page.locator("#weekly-form button").first().isDisabled(), true);
    assert.match(await page.locator("#report-period").innerText(), /غير مخول/);
  }, { weeklyDelay: 350 });
  await test("double-click save creates one request and locks identity", async ({ page, calls }) => {
    await page.locator("#center-name").fill("مركز جديد"); await page.locator("#center-rate").fill("40");
    await page.locator("#center-create").dblclick();
    assert.equal(await page.locator("#actor").isDisabled(), true);
    await page.getByText("تمت إضافة مركز العمل.", { exact: true }).waitFor();
    assert.equal(calls.filter(c => c.path === "/work-centers" && c.method === "POST").length, 1);
    assert.equal(await page.locator("#actor").isDisabled(), false);
  }, { mutationDelay: 350 });
  await test("network failure gives Arabic message and refresh recovers", async ({ page, options }) => {
    assert.match(await page.locator("#notice").innerText(), /تعذر الاتصال بالخادم/);
    options.networkFailure = false;
    await page.locator("#refresh-centers").click();
    await page.locator("#centers-body input").first().waitFor();
    assert.equal(await page.locator("#notice").isHidden(), true);
  }, { networkFailure: true });
  await test("HTML gateway error is not a JSON exception", async ({ page }) => {
    await tab(page, "weekly"); await page.locator("#week-start").fill("2026-10-12");
    await page.locator("#weekly-form button").first().click();
    await page.locator("#notice[data-kind=error]").waitFor();
    assert.match(await page.locator("#notice").innerText(), /503/);
    assert.doesNotMatch(await page.locator("#notice").innerText(), /Unexpected|gateway|<h1>/);
    assert.equal(await page.locator("#weekly-form button").first().isEnabled(), true);
  }, { reportFailure: true });
  await test("CSV downloads and rejects invalid or blank dates", async ({ page, calls }) => {
    await tab(page, "weekly"); await page.locator("#week-start").fill("2026-10-12");
    const pending = page.waitForEvent("download");
    await page.locator("#weekly-csv").click();
    assert.equal((await pending).suggestedFilename(), "fde-weekly-2026-10-12.csv");
    for (const value of ["2026-10-13", ""]) {
      await page.locator("#week-start").fill(value); await page.locator("#weekly-csv").click();
    }
    assert.equal(calls.filter(c => c.path === "/reports/weekly.csv").length, 1);
  });
  await test("allocation form selects distinct containers and guards duplicates", async ({ page, calls }) => {
    await role(page, "acc1"); await tab(page, "landed");
    await page.locator("#allocation-lines select").first().waitFor();
    await page.locator("#add-allocation-line").click();
    const selected = await page.locator("#allocation-lines select").evaluateAll(els => els.map(el => el.value));
    assert.equal(selected.join(","), "1,2");
    assert.equal(await page.locator("#add-allocation-line").isDisabled(), true);
    await page.locator("#allocation-lines select").nth(1).selectOption("1");
    await page.locator("#source-reference").fill("REF-UI");
    await page.locator("#freight").fill("1");
    await page.locator("#allocation-create").click();
    await page.locator("#notice[data-kind=error]").waitFor();
    assert.match(await page.locator("#notice").innerText(), /لا يمكن تكرار الحاوية/);
    assert.equal(calls.filter(c => c.path === "/landed-costs" && c.method === "POST").length, 0);
  });
  await test("empty units disable allocation instead of sending invalid payload", async ({ page }) => {
    await role(page, "acc1"); await tab(page, "landed");
    assert.equal(await page.locator("#allocation-create").isDisabled(), true);
    assert.equal(await page.locator("#add-allocation-line").isDisabled(), true);
    assert.equal(await page.locator("#allocation-lines select").count(), 0);
  }, { emptyUnits: true });
  await test("late allocation response cannot leak into warehouse view", async ({ page }) => {
    await tab(page, "landed"); await role(page, "wh1"); await page.waitForTimeout(450);
    assert.equal(await page.locator(".allocation-card").count(), 0);
    assert.match(await page.locator("#allocations-list").innerText(), /غير مخول/);
  }, { allocationDelay: 300 });
  await test("stale version is recoverable and never announced as saved", async ({ page }) => {
    await page.locator("#centers-body input").first().fill("تعديل");
    await page.locator("#centers-body tr").first().getByRole("button", { name: "حفظ", exact: true }).click();
    await page.locator("#notice[data-kind=error]").waitFor();
    assert.match(await page.locator("#notice").innerText(), /جلسة أخرى/);
    assert.equal(await page.locator("#actor").isEnabled(), true);
  }, { conflict: true });
  await test("mobile/table scrolling, dark mode and auditor permissions", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    for (const name of ["centers", "landed", "weekly"]) {
      await tab(page, name);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    await page.locator("#theme-toggle").click();
    assert.equal(await page.locator("body").evaluate(el => el.classList.contains("dark")), true);
    await role(page, "aud1"); await tab(page, "centers"); await page.locator("#centers-body input").first().waitFor();
    assert.equal(await page.locator("#center-create").isDisabled(), true);
    assert.equal(await page.locator("#centers-body input").first().isDisabled(), true);
  });
  return { passed: results.filter(r => r.passed).length, failed: results.filter(r => !r.passed).length, results };
}

if (typeof process !== "undefined" && process.argv[1] &&
    import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try {
    const results = await runOperationsQA(browser, process.env.FDE_QA_URL);
    console.log(JSON.stringify(results, null, 2));
    if (results.failed) process.exitCode = 1;
  } finally { await browser.close(); }
}
