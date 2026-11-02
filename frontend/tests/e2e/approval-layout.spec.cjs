const { test, expect } = require("@playwright/test");

const supplier = "Yukato Japan Industrial Development Limited";
const batch = () => ({
  kind: "batch_create",
  count: 6,
  rows: [],
  extra: 0,
  records: ["Supplier", "Address", "Contact", "Item", "Item", "Item"].map(
    (doctype, i) => ({
      doctype,
      name:
        i === 0 ? supplier : `${doctype}-${i}-` + "LongRecordName".repeat(8),
      rows: [
        { label: `${doctype} Name`, value: supplier },
        ...Array.from({ length: 7 }, (_, j) => ({
          label: `Field ${j}`,
          value: `Synthetic value ${j}`,
        })),
        {
          label: "Details",
          value:
            "Manufacturer and exporter. Terms are FOB; see contract. ".repeat(
              8
            ),
        },
      ],
      extra: 0,
      tables: [],
    })
  ),
});

async function openBoard(page, baseURL, sheet = false, options = {}) {
  const calls = [],
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page
    .context()
    .addCookies([
      { name: "user_id", value: "review%40example.test", url: baseURL },
    ]);
  await page.addInitScript(() =>
    Object.assign(window, {
      csrf_token: "test",
      time_zone: "Asia/Kolkata",
      agent_name: "Jarvis",
      is_system_manager: false,
      is_jarvis_admin: false,
      support_available: false,
      has_support_access: false,
    })
  );
  const kind = options.chat
    ? "chat"
    : sheet
    ? "file_box_sheet"
    : "file_box_held";
  const row = {
    name: "PA-LAYOUT",
    kind,
    status: "Pending",
    summary: "New records: Supplier " + supplier,
    waiters_count: 1,
    document_type: "Supplier",
  };
  const records = ["Supplier", "Address", "Item", "UOM"].map((doctype, i) => ({
    index: i,
    doctype,
    name: "TEST-" + i,
    title: supplier,
    category: ["party", "address", "item", "other"][i],
    op: "update",
    values: { supplier_name: supplier },
    depends_on: [],
    needs_fix: {
      field: "supplier_name",
      label: "Name",
      message: "Check this field",
    },
    needs_input: [],
    secret: [],
    locked: {},
  }));
  await page.route("**/api/method/**", async (route) => {
    const req = route.request(),
      method = new URL(req.url()).pathname.split("/api/method/")[1];
    let message = {};
    if (method.endsWith("is_ready_for_chat")) message = { ready: true };
    else if (method.endsWith("list_pending_actions_lane"))
      message = {
        rows: Array.from({ length: options.rowCount || 1 }, (_, i) => ({
          ...row,
          name: i ? `PA-LAYOUT-${i}` : row.name,
        })),
      };
    else if (method.endsWith("get_pending_action"))
      message = sheet
        ? {
            ...row,
            collecting: 0,
            can_act: 1,
            record_count: 4,
            question_count: 0,
            card_sha256: "layout-sha",
            records,
            questions: [],
            errors: {},
            decisions: {},
            outcome: {},
          }
        : {
            ...row,
            card: batch(),
            doctype: "Supplier",
            can_act: 1,
            can_edit: 1,
            candidates: [],
            needs_input: [],
            docs: batch().records.map((r) => ({
              doctype: r.doctype,
              values: { supplier_name: r.name },
              secret: [],
              locked: {},
            })),
          };
    else if (method.endsWith("get_doctype_form_meta"))
      message = {
        ok: true,
        fields: [
          { fieldname: "supplier_name", label: "Name", fieldtype: "Data" },
        ],
        tables: {},
      };
    else if (method.endsWith("list_approvals_page"))
      message = { rows: [], total: 0, facets: {} };
    else if (method.endsWith("decide_held_action")) {
      calls.push(JSON.parse(req.postData()));
      message = { ok: false, reason_code: "busy" };
    } else if (method.endsWith("get_chat_ui_settings"))
      message = { time_zone: "Asia/Kolkata" };
    else if (method.includes("count")) message = 0;
    else if (method.includes("list")) message = [];
    await route.fulfill({ json: { message } });
  });
  await page.goto("/jarvis/approvals?held=PA-LAYOUT&nosocket");
  await expect(
    page.getByRole("button", {
      name: options.chat
        ? "Confirm"
        : sheet
        ? "Apply & continue"
        : "Create & continue",
      exact: true,
    })
  ).toBeAttached({ timeout: 30000 });
  return { calls, errors };
}

async function pointerReachable(page, element) {
  await element.scrollIntoViewIfNeeded();
  await expect(element).toBeVisible();
  const hit = await element.evaluate((el) => {
    const r = el.getBoundingClientRect(),
      target = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return !!target && (target === el || el.contains(target));
  });
  expect(hit).toBe(true);
}

for (const width of [1280, 1024, 768, 390]) {
  test(`expanded Board records stay in flow and reachable at ${width}px`, async ({
    page,
    baseURL,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const { calls, errors } = await openBoard(page, baseURL);
    const recs = page.locator(".jv-pcard-recs"),
      details = recs.locator("details.jv-rec");
    await expect(details).toHaveCount(6);
    for (let i = 0; i < 3; i++)
      if ((await details.nth(i).getAttribute("open")) === null)
        await details.nth(i).locator("summary").click();
    expect(
      await recs.evaluate((e) => e.scrollHeight - e.clientHeight)
    ).toBeLessThanOrEqual(1);
    for (let i = 0; i < 3; i++) {
      const bottom = await details
          .nth(i)
          .locator(".jv-rec-body")
          .evaluate((e) => e.getBoundingClientRect().bottom),
        top = await details
          .nth(i + 1)
          .locator("summary")
          .evaluate((e) => e.getBoundingClientRect().top);
      expect(bottom).toBeLessThanOrEqual(top + 1);
    }
    await pointerReachable(page, details.last().locator("summary"));
    await details.last().locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(details.last()).toHaveAttribute("open", "");
    await page.keyboard.press("Enter");
    await expect(details.last()).not.toHaveAttribute("open", "");
    const create = page.getByRole("button", {
      name: "Create & continue",
      exact: true,
    });
    await pointerReachable(page, create);
    await pointerReachable(
      page,
      page.getByRole("button", { name: "Edit & create", exact: true })
    );
    const skip = page.getByRole("button", {
      name: "Don't create, skip 1 file",
      exact: true,
    });
    await pointerReachable(page, skip);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBe(true);
    const b = await recs.boundingBox();
    expect(b.x).toBeGreaterThanOrEqual(0);
    expect(b.x + b.width).toBeLessThanOrEqual(width + 1);
    expect(b.width).toBeGreaterThanOrEqual(300);
    expect(calls).toEqual([]);
    await skip.click();
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toMatchObject({ name: "PA-LAYOUT", action: "skip" });
    await expect(
      page.getByText(
        "Someone is acting on this approval right now. Try again in a moment.",
        { exact: false }
      )
    ).toBeVisible();
    const screenshot = testInfo.outputPath("board.png");
    await page.screenshot({ path: screenshot });
    await testInfo.attach(`Board records ${width}`, {
      path: screenshot,
      contentType: "image/png",
    });
    expect(errors).toEqual([]);
  });
}

test("the shared card outside the Board keeps its compact scrolling", async ({
  page,
  baseURL,
}) => {
  await openBoard(page, baseURL);
  const card = batch();
  const sizes = await page.evaluate(async (card) => {
    const { createApp, h } = await import("/node_modules/.vite/deps/vue.js"),
      { default: PendingCard } = await import(
        "/src/components/PendingCard.vue"
      );
    const el = document.createElement("div");
    el.style.cssText = "position:fixed;top:0;left:0;width:700px";
    document.body.append(el);
    const app = createApp({ render: () => h(PendingCard, { card }) });
    app.mount(el);
    await new Promise((r) => requestAnimationFrame(r));
    const recs = el.querySelector(".jv-pcard-recs"),
      result = { height: recs.clientHeight, scrollHeight: recs.scrollHeight };
    app.unmount();
    el.remove();
    return result;
  }, card);
  expect(sizes.height).toBeLessThanOrEqual(320);
  expect(sizes.scrollHeight).toBeGreaterThan(sizes.height);
});

test("sheet section titles and attention stay separate at phone width", async ({
  page,
  baseURL,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await openBoard(page, baseURL, true);
  const sections = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { level: 3 }) });
  await expect(sections).toHaveCount(4);
  for (const section of await sections.all()) {
    const heading = section.getByRole("heading", { level: 3 }),
      attention = section.getByText("1 needs attention", { exact: true });
    await heading.scrollIntoViewIfNeeded();
    const a = await heading.boundingBox(),
      b = await attention.boundingBox();
    expect(a.width).toBeGreaterThan(0);
    expect(
      a.x + a.width <= b.x + 1 ||
        b.x + b.width <= a.x + 1 ||
        a.y + a.height <= b.y + 1 ||
        b.y + b.height <= a.y + 1
    ).toBe(true);
  }
  const screenshot = testInfo.outputPath("headings.png");
  await page.screenshot({ path: screenshot });
  await testInfo.attach("Sheet section headings", {
    path: screenshot,
    contentType: "image/png",
  });
});

test("chat-originated Board cards also expand in the detail pane", async ({
  page,
  baseURL,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const { errors, calls } = await openBoard(page, baseURL, false, {
    chat: true,
  });
  const recs = page.locator(".jv-pcard-recs"),
    details = recs.locator("details.jv-rec");
  for (let i = 1; i < 3; i++) await details.nth(i).locator("summary").click();
  expect(
    await recs.evaluate((e) => e.scrollHeight - e.clientHeight)
  ).toBeLessThanOrEqual(1);
  await pointerReachable(page, details.last().locator("summary"));
  await pointerReachable(
    page,
    page.getByRole("button", { name: "Confirm", exact: true })
  );
  await pointerReachable(
    page,
    page.getByRole("button", { name: "Discard", exact: true })
  );
  expect(calls).toEqual([]);
  expect(errors).toEqual([]);
  const screenshot = testInfo.outputPath("chat.png");
  await page.screenshot({ path: screenshot });
  await testInfo.attach("Chat-originated Board card", {
    path: screenshot,
    contentType: "image/png",
  });
});

test("a populated phone rail stays bounded and scrolls independently", async ({
  page,
  baseURL,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await openBoard(page, baseURL, false, { rowCount: 30 });
  const group = page.locator('[aria-labelledby="actions-title"]'),
    rail = group.locator("..");
  const size = await rail.evaluate((el) => ({
    height: el.clientHeight,
    scrollHeight: el.scrollHeight,
    available: el.parentElement.clientHeight,
  }));
  expect(size.scrollHeight).toBeGreaterThan(size.height);
  expect(size.height).toBeLessThanOrEqual(size.available * 0.4);
  await expect(group.locator("#actions-body button")).toHaveCount(30);
  const last = group.locator("#actions-body button").last();
  await pointerReachable(page, last);
  await pointerReachable(
    page,
    page.getByRole("button", { name: "Create & continue", exact: true })
  );
  const screenshot = testInfo.outputPath("populated-rail.png");
  await page.screenshot({ path: screenshot });
  await testInfo.attach("Populated phone rail", {
    path: screenshot,
    contentType: "image/png",
  });
});
