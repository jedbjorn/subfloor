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
  let provisionReads = 0;
  let provisionComplete = false;
  let removalWrites = 0;
  let removalReads = 0;
  let inventoryReads = 0;
  let publication = "list unavailable";
  let finishFirst;
  const firstInventory = new Promise(resolve => { finishFirst = resolve; });
  await page.route("http://manager.test/**", async route => {
    const url = new URL(route.request().url()).pathname;
    const json = data => route.fulfill({json: data});
    if (url === "/api/auth/status") return json({enrolled: true, signed_in: true, session: null});
    if (url === "/api/actions/preview") {
      const {action, name} = route.request().postDataJSON();
      if (action === "remove") {
        return json({name: "sample", action: "remove", id: "rm", detail: "Remove sample", phrase: "REMOVE sample"});
      }
      if (name === "occupied") {
        return route.fulfill({status: 400, json: {error: "Name already occupied"}});
      }
      return json({name: "sample", action: "create", id: "op", detail: "Create sample", phrase: ""});
    }
    if (url === "/api/actions/apply") {
      if (route.request().postDataJSON().id === "rm") {
        removalWrites++;
        return json({id: "rm", status: "running"});
      }
      applied = true;
      return json({id: "op", status: "running", stage: "Confirmed"});
    }
    if (url === "/api/actions/status/op") {
      if (++provisionReads === 2) {
        return route.fulfill({status: 503, json: {error: "Status offline"}});
      }
      return json({id: "op", status: provisionComplete ? "succeeded" : "running", stage: "Install Subfloor"});
    }
    if (url === "/api/actions") {
      if (removalWrites) {
        removalReads++;
        if (removalReads === 1) return route.fulfill({status: 503, json: {error: "History offline"}});
        if (removalReads > 2) return json([{id: "rm", action: "remove", status: "succeeded", path: target, created}]);
      }
      return json(applied ? [{id: "op", action: "create", status: "succeeded",
        path: target, created}] : []);
    }
    if (url === "/api/inventory") {
      inventoryReads++;
      if (removalReads === 3) return route.fulfill({status: 503, json: {error: "Retry inventory"}});
      if (removalReads > 3) return json({root: "/repos", installations: []});
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
  await page.getByLabel("Repository name").fill("occupied");
  await page.getByLabel("Subfloor operator username").fill("Ada");
  await page.getByRole("button", {name: "Preview installation"}).click();
  const setup = page.getByRole("dialog", {name: "Add repository"});
  await setup.getByText("Name already occupied").waitFor();
  assert.equal(await setup.getByLabel("Subfloor operator username").inputValue(), "Ada");
  await page.getByLabel("Repository name").fill("sample");
  await page.getByRole("button", {name: "Preview installation"}).click();
  await page.getByRole("button", {name: "Confirm", exact: true}).click();
  const progress = page.getByRole("dialog", {name: "Create repository · sample"});
  await progress.getByText("Confirmed…", {exact: true}).waitFor();
  await page.clock.fastForward(1100);
  await progress.getByText("Install Subfloor…", {exact: true}).waitFor();
  await page.keyboard.press("Escape");
  assert.equal(await progress.isVisible(), true);
  assert.equal(await progress.getByRole("button", {name: "Close"}).isDisabled(), true);
  await page.clock.fastForward(1100);
  await progress.getByText(/Reconnecting to the Manager/).waitFor();
  provisionComplete = true;
  await page.clock.fastForward(1100);
  await progress.getByText("Subfloor is installed and running.").waitFor();
  await progress.getByRole("button", {name: "Close"}).click();
  finishFirst();
  await page.getByText("Inventory offline").waitFor();
  await page.getByText("Create repository · succeeded").waitFor();
  const row = page.locator('tr[data-repo="sample"]');
  for (const state of ["list unavailable", "unavailable", "verified"]) {
    publication = state;
    await page.clock.fastForward(3100);
    await row.getByText(state[0].toUpperCase() + state.slice(1), {exact: true}).waitFor();
  }
  assert.equal(await row.getByRole("link", {name: "sample", exact: true}).getAttribute("href"), verified);
  assert.equal(await row.getByRole("link", {name: "Open", exact: true}).count(), 0);
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

  await row.getByRole("button", {name: "Actions for sample"}).click();
  await row.getByRole("menuitem", {name: "Remove Subfloor from this repo"}).click();
  await page.getByLabel("Type REMOVE sample to confirm removal").fill("REMOVE sample");
  await page.getByRole("button", {name: "Confirm", exact: true}).click();
  await page.getByText("History offline").waitFor();
  // A stale history read, then a terminal result with unavailable inventory.
  await page.clock.fastForward(3100);
  await row.waitFor();
  await page.clock.fastForward(3100);
  await page.getByText("Retry inventory").waitFor();
  await page.clock.fastForward(3100);
  await page.getByText("No installations found.").waitFor();
  await page.getByText("Remove Subfloor from this repo · succeeded").waitFor();
  await page.clock.fastForward(3100);
  assert.equal(removalWrites, 1);
  assert.equal(removalReads, 4);
}

(async () => {
  const browser = await chromium.launch({executablePath: process.argv[3], headless: true});
  try {
    await exercise(await browser.newPage());
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
