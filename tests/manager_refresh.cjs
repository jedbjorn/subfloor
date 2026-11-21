// Hold the first inventory response across Apply, then allow the published
// route to recover. All requests, including the verified link, are fixtures.
const { chromium } = require(process.argv[2]);
const path = require("node:path");
const assert = require("node:assert/strict");

async function exercise(page) {
  const target = "/repos/sample";
  const verified = "https://sample.test";
  const created = Date.now() / 1000;
  let applied = false;
  let inventoryReads = 0;
  let publication = "list unavailable";
  let finishFirst;
  const firstInventory = new Promise(resolve => { finishFirst = resolve; });
  await page.route("http://manager.test/**", async route => {
    const url = new URL(route.request().url()).pathname;
    const json = data => route.fulfill({json: data});
    if (url === "/api/actions/preview") {
      return json({name: "sample", action: "create", id: "op", detail: "Create sample", phrase: ""});
    }
    if (url === "/api/actions/apply") {
      applied = true;
      return json({});
    }
    if (url === "/api/actions") {
      return json(applied ? [{id: "op", action: "create", status: "succeeded",
        path: target, created}] : []);
    }
    if (url === "/api/inventory") {
      inventoryReads++;
      if (inventoryReads === 1) {
        await firstInventory;
        return json({root: "/repos", installations: []});
      }
      if (inventoryReads === 2) return route.fulfill({status: 503, json: {error: "Inventory offline"}});
      return json({root: "/repos", installations: [
        {name: "sample", path: target, runtime: "host", configured_runtime: "host", state: "running",
          health: "healthy", publication, open_url: publication === "verified" ? verified : null},
        {name: "neighbor", path: "/repos/neighbor", runtime: "host", configured_runtime: "host", state: "running",
          health: "healthy", publication: "pending"}]});
    }
    return route.fulfill({path: path.join(process.argv[4], url === "/" ? "index.html" : url.slice(1))});
  });
  await page.clock.install();
  const firstRequest = page.waitForRequest(req => req.url().endsWith("/api/inventory"));
  await page.goto("http://manager.test/");
  await firstRequest;
  await page.getByRole("button", {name: "Add repository"}).click();
  await page.getByLabel("Destination").selectOption("create");
  await page.getByLabel("Repository name").fill("sample");
  await page.getByLabel("Subfloor operator username").fill("Ada");
  await page.getByRole("button", {name: "Preview installation"}).click();
  await page.getByRole("button", {name: "Confirm", exact: true}).click();
  await page.getByRole("dialog").waitFor({state: "hidden"});
  finishFirst();
  await page.getByText("Inventory offline").waitFor();
  await page.getByText("Create repository · succeeded").waitFor();
  const row = page.locator('tr[data-repo="sample"]');
  for (const state of ["list unavailable", "unavailable", "verified"]) {
    publication = state;
    await page.clock.fastForward(3100);
    await row.getByText(state[0].toUpperCase() + state.slice(1), {exact: true}).waitFor();
  }
  assert.equal(await row.getByRole("link", {name: "Open"}).getAttribute("href"), verified);
  await page.clock.fastForward(3100);
  assert.equal(inventoryReads, 5);
  // Reopen the publication window, then jump beyond its deadline.
  publication = "pending";
  await page.getByRole("button", {name: "Refresh"}).click();
  await row.getByText("Pending", {exact: true}).waitFor();
  await page.clock.fastForward(121000);
  await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
  await page.clock.fastForward(3100);
  assert.equal(inventoryReads, 7);
}

(async () => {
  const browser = await chromium.launch({executablePath: process.argv[3], headless: true});
  try {
    await exercise(await browser.newPage());
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
