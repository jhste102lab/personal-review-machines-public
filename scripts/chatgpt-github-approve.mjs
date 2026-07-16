#!/usr/bin/env node
import { pathToFileURL } from "node:url";

const home = process.env.PRM_HOME_OVERRIDE || process.env.HOME || "/tmp";
const cloakDir = process.env.PRM_CLOAK_DIR || `${home}/.local/share/personal-review-machines/cloakbrowser`;
const port = process.env.PRM_CDP_PORT || "9222";
const cdpUrl = `http://127.0.0.1:${port}`;
const allowLabels = [
  "허용하기",
  "허용",
  "Allow",
  "Continue",
  "계속",
  "Confirm",
  "확인",
];

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

let browser;
let approved = 0;
try {
  browser = await chromium.connectOverCDP(cdpUrl, { timeout: 10_000 });
  const pages = browser.contexts().flatMap((context) => context.pages());
  for (const page of pages) {
    const pageHint = await pageHintFor(page);
    for (const label of allowLabels) {
      const buttons = page.getByRole("button", { name: label });
      const count = await buttons.count().catch(() => 0);
      for (let index = 0; index < count; index += 1) {
        const button = buttons.nth(index);
        if (!(await button.isVisible().catch(() => false))) {
          continue;
        }
        const box = await button.boundingBox().catch(() => null);
        if (!box || box.width < 20 || box.height < 20) {
          continue;
        }
        const contextText = await page.locator("body").innerText({ timeout: 3000 }).catch(() => "");
        if (!/GitHub|github/i.test(contextText)) {
          continue;
        }
        await button.click({ timeout: 5000 });
        approved += 1;
        console.log(`approved github permission on ${pageHint} via "${label}"`);
        await page.waitForTimeout(1200);
      }
    }
  }
  console.log(JSON.stringify({ ok: true, approved, cdpUrl }));
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  if (browser) {
    await browser.close().catch(() => {});
  }
}

async function pageHintFor(page) {
  const url = page.url();
  const text = await page.locator("body").innerText({ timeout: 3000 }).catch(() => "");
  const pr = text.match(/PR 번호: #(\d+)/)?.[1] || text.match(/PR #(\d+)/)?.[1];
  return pr ? `pr-${pr}` : url;
}