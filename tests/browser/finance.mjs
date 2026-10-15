/**
 * Real UI/API cycle. Only use the isolated seeded QA server on loopback:8770.
 * This test writes demo expenses and financial closes, never production data.
 */
const { default: assert } = await import("node:assert/strict");
const { pathToFileURL } = await import("node:url");

export async function runFinanceCycleQA(browser, url = "http://127.0.0.1:8770/operations") {
  const target = new URL(url);
  assert.ok(target.hostname === "127.0.0.1" && target.port === "8770",
    "Finance write QA must target the isolated loopback:8770 server");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], posts = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", req => { if (req.method() === "POST") posts.push(new URL(req.url()).pathname); });
  const role = async actor => {
    await page.locator("#actor").selectOption(actor);
    await page.waitForFunction(actor => state.session?.username === actor, actor);
    if (await page.locator("#finance").isVisible())
      await page.waitForFunction(() => document.querySelector("#settlements-list").textContent !== "جار تحميل التسويات…");
  };
  const check = async () => {
    await page.locator("#close-unit").selectOption("1");
    await page.locator("#close-check").click();
    await page.locator("#close-results table").waitFor();
  };
  try {
    await page.goto(url);
    await page.locator("#centers-body input").first().waitFor();
    await role("acc1");
    await page.locator("nav [data-panel=finance]").click();
    await page.locator("#settlement-invoice option[value='1']").waitFor({ state: "attached" });
    await page.locator("#settlement-create").click();
    await page.locator("#settlements-list article").first().waitFor();
    assert.equal(await page.locator("#settlements-list form").count(), 0);
    await page.locator("#expense-unit").selectOption("1");
    await page.locator("#expense-category").selectOption("EQUIPMENT");
    await page.locator("#expense-reference").fill("UI-EQ-001");
    await page.locator("#expense-date").fill("2026-10-15");
    await page.locator("#expense-net").fill("100.01");
    await page.locator("#expense-tax").fill("5");
    await page.locator("#expense-description").fill("معدات دورة اختبار الواجهة المعزولة");
    await page.locator("#expense-create").dblclick();
    await page.locator("#expenses-list article").first().waitFor();
    assert.equal(posts.filter(p => p === "/api/v1/unit-expenses").length, 1);
    await role("gm1");
    const approve = async list => {
      const form = page.locator(`#${list} article form`).first();
      await form.locator("input").fill("اعتماد مستند الاختبار التجريبي");
      await form.locator("button").click();
      await page.locator(`#${list} article`).first().filter({ hasText: "معتمد" }).waitFor();
    };
    await approve("settlements-list"); await approve("expenses-list");
    await check();
    assert.match(await page.locator("#close-results").innerText(), /11528\.51/);
    // Native confirmation must allow backing out without a financial POST.
    await page.locator("#close-note").fill("مراجعة قبل الإقفال المالي");
    page.once("dialog", dialog => dialog.dismiss());
    await page.locator("#financial-close-button").click();
    assert.equal(posts.filter(p => p.endsWith("/financial-close")).length, 0);
    await role("fin1"); await check();
    await page.locator("#close-note").fill("إقفال دورة اختبار المتصفح المعزولة");
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#financial-close-button").click();
    await page.locator("#close-results h3").filter({ hasText: "مقفل مالياً" }).waitFor();
    assert.equal(await page.locator("#financial-reopen-button").isDisabled(), true);
    await role("gm1"); await check();
    await page.locator("#close-note").fill("إعادة فتح تجريبية لإثبات حفظ سجل الإقفال");
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#financial-reopen-button").click();
    await page.locator("#close-results h3").filter({ hasText: "جاهز للإقفال" }).waitFor();
    await page.locator("#close-note").fill("إقفال الإصدار الثاني بعد إعادة الفتح");
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#financial-close-button").click();
    await page.locator("#close-results h3").filter({ hasText: "إصدار 2" }).waitFor();
    assert.equal(await page.locator("#close-history details").count(), 2);
    await page.locator("#close-history details").first().locator("summary").click();
    await page.screenshot({ path: "/tmp/fde-finance-ui-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#theme-toggle").click();
    await page.screenshot({ path: "/tmp/fde-finance-ui-dark-mobile.png", fullPage: true });
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `No document overflow at ${width}`);
    }
    await role("wh1");
    await page.locator("#close-results").filter({ hasText: "غير مخول" }).waitFor();
    assert.equal(await page.locator("#expenses-list article").count(), 0);
    assert.equal(await page.locator("#expense-create").isDisabled(), true);
    assert.deepEqual(errors, []);
    return { passed: true, checks: [
      "invoice settlement preparation and independent approval",
      "equipment expense + nonrecoverable tax, duplicate click creates one entry",
      "independently calculated 11,528.51 SAR displayed",
      "confirmation dismissal creates no close",
      "close locks snapshot; same closer cannot reopen",
      "independent reopen and second immutable close revision",
      "history, dark/mobile, widths 320/390/768/1440",
      "warehouse role clears financial data and disables writes",
    ], errors };
  } finally { await page.close(); }
}

if (typeof process !== "undefined" && process.argv[1] &&
    import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try { console.log(JSON.stringify(await runFinanceCycleQA(browser), null, 2)); }
  finally { await browser.close(); }
}
