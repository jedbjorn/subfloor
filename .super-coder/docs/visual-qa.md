# Visual QA evidence

Visual QA is opt-in through tracked, fork-owned `.sc-state/visual-qa.json`.
Without configuration, `ci` returns a neutral result without installing a
browser or booting an app. Existing route captures and their advisory failure
rules are unchanged: all routes failing fails CI; partial route failures are
shown for review. Appearance is judged by the reviewer.

## Set up CI

Configure the app with `sc visual-qa init`, review the configuration, then run
`sc visual-qa setup-ci`. Review and commit the two generated workflows.
Setup refuses to overwrite files or add a competing capture lane when an
existing workflow calls `visual-qa ci`. Adapt an existing lane from the templates
under `.super-coder/templates/visual-qa/` instead.

The publisher workflow must be on the default branch before PR captures can
trigger it. It runs the default branch's pinned engine, so that engine must
support `visual-qa publish`; a PR's newer engine pin does not upgrade the trusted
publisher. Normal install/update never installs or rewrites these workflows.

- Capture executes PR code in an isolated GitHub-hosted job with `contents: read`.
  It invokes `ci --no-publish --report .sc-state/local/visual-qa-report` and uploads
  this portable report separately from the full diagnostic gallery.
- Publication runs on `workflow_run` using trusted default-branch code and
  `actions: read`, `contents: write`, `pull-requests: write`. It downloads only
  the originating run's report, validates PNG files and manifest, verifies the
  PR's current head and run identity, and publishes one updating bot comment.
- External GitHub-fork PRs keep artifact-only QA; the supplied publisher does
  not give their capture jobs a write token. This differs from a Subfloor fork,
  which is an application repository with its own normal same-repo PRs.
- The publisher writes PNGs and a sanitized manifest to the independent
  `qa-evidence/pr-<number>` branch. It never commits app code there. Do not merge
  that branch. Image links identify the evidence commit, not a moving branch.
- Publication failure makes the publisher job fail while capture artifacts
  remain available. Browser assertions cannot become green through an upload.
  The report names the PR head, actual tested checkout (which can be GitHub's
  merge commit), source workflow run/attempt, viewport dimensions and data mode.
- Evidence branches are retained after merge/closure. Artifact expiry does not
  delete them. Deleting evidence refs can break historical image access; choose
  a repository retention policy before removing them. Binary evidence grows
  repository storage; normal captures are bounded to 100 images, 8 MiB each,
  and 64 MiB combined. The JSON manifest is bounded to 1 MiB.

## Named interaction states

For interactive screens, add an optional `capture_command` to the existing
configuration. This replaces the built-in route capture loop with fork-owned
Playwright code; app startup, gallery generation and CI publication stay in
Subfloor. The fork supplies its runner/browser dependencies in `setup`.

```json
{
  "cwd": "ui",
  "setup": ["npm ci", "npx playwright install --with-deps chromium", "npm run build"],
  "serve": "npm run preview -- --port {port} --host 127.0.0.1",
  "capture_command": "node tests/visual-qa.mjs",
  "capture_timeout_s": 300,
  "viewports": [{"name": "desktop", "width": 1440, "height": 900}],
  "paths": ["ui/src/**", "ui/tests/**", "ui/package*.json"]
}
```

Configuration changes always trigger capture even if `paths` excludes the
config. Include fixture, scenario and shared style paths in `paths`. A changed
path set that cannot be resolved causes capture rather than an unsafe skip.
The engine does not infer a component-to-route dependency graph.

The command receives:

| Environment | Value |
|---|---|
| `SC_VISUAL_QA_URL` | Ready app URL |
| `SC_VISUAL_QA_OUTPUT` | Absolute output directory |
| `SC_VISUAL_QA_VIEWPORTS` | JSON array of configured viewports |
| `SC_VISUAL_QA_ROUTES` | JSON array of routes, or `[]` |

Write PNGs and `states.json` under the output directory. Use stable state and
variant names. Each state can contain a different set of variants. Manifest
version 1 is:

```json
{
  "version": 1,
  "data_mode": "stubbed",
  "states": [{
    "name": "Confirmation names the decision",
    "captures": [{
      "name": "Dark / desktop",
      "ok": true,
      "image": "confirmation-dark-desktop.png",
      "viewport_width": 1440,
      "viewport_height": 900
    }]
  }]
}
```

`data_mode` is `stubbed`, `real`, or `static`. A stub proves UI behavior, not
production API authorization. Built-in route capture reports `unspecified`.
On assertion failure, emit a capture with `ok: false`, a concise `error`, and
an image if available; otherwise `image: null`. Write the manifest after each
checkpoint so later failures retain completed evidence. A nonzero command exit,
timeout, invalid manifest, or any failed state fails the scenario check.

Example checkpoint helper within an ordinary Playwright script:

```js
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from '@playwright/test';

const out = process.env.SC_VISUAL_QA_OUTPUT;
const report = { version: 1, data_mode: 'stubbed', states: [] };
async function checkpoint(page, state, variant, image, viewport) {
  await page.screenshot({ path: path.join(out, image), fullPage: true });
  let row = report.states.find(row => row.name === state);
  if (!row) report.states.push(row = { name: state, captures: [] });
  row.captures.push({ name: variant, ok: true, image,
    viewport_width: viewport.width, viewport_height: viewport.height });
  await writeFile(path.join(out, 'states.json'), JSON.stringify(report));
}

const browser = await chromium.launch();
try {
  for (const viewport of JSON.parse(process.env.SC_VISUAL_QA_VIEWPORTS)) {
    const page = await browser.newPage({ viewport });
    // Install your deterministic API fixtures and theme before navigating.
    await page.goto(process.env.SC_VISUAL_QA_URL);
    // Use Playwright locators and assertions to exercise each app-specific state.
    await checkpoint(page, 'Initial list', `Dark / ${viewport.name}`,
      `list-dark-${viewport.name}.png`, viewport);
    await page.close();
  }
} finally {
  await browser.close();
}
```

Use the same command locally through `sc visual-qa run --url <app-url>`.
No image baseline, pixel diff, AI scoring, or model call is part of this pipeline.
