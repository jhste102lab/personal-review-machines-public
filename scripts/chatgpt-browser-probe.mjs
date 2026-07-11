#!/usr/bin/env node
import { pathToFileURL } from "node:url";

const home = process.env.PRM_HOME_OVERRIDE || process.env.HOME || "/tmp";
const cloakDir = process.env.PRM_CLOAK_DIR || `${home}/.local/share/personal-review-machines/cloakbrowser`;
const port = process.env.PRM_CDP_PORT || "9222";
const cdpUrl = `http://127.0.0.1:${port}`;

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

let browser;
try {
  browser = await chromium.connectOverCDP(cdpUrl, { timeout: 10_000 });
  const contexts = browser.contexts();
  const pages = contexts.flatMap((context) => context.pages());
  if (!contexts.length || !pages.length) {
    throw new Error("ChatGPT browser has no usable context or page");
  }
  console.log(`healthy ${cdpUrl}`);
} finally {
  if (browser) {
    await browser.close().catch(() => {});
  }
}
