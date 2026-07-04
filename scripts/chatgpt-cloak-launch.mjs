#!/usr/bin/env node
import { pathToFileURL } from "node:url";

const userDataDir = process.env.PRM_BROWSER_PROFILE_DIR;
const cdpPort = process.env.PRM_CDP_PORT || "9222";
const chatgptUrl = process.env.PRM_CHATGPT_URL || "https://chatgpt.com/";
const cloakDir = process.env.PRM_CLOAK_DIR;

if (!userDataDir) {
  throw new Error("PRM_BROWSER_PROFILE_DIR is required");
}
if (!cloakDir) {
  throw new Error("PRM_CLOAK_DIR is required");
}

const cloakbrowser = await import(
  pathToFileURL(`${cloakDir}/node_modules/cloakbrowser/dist/index.js`).href
);

const context = await cloakbrowser.launchPersistentContext({
  userDataDir,
  headless: false,
  humanize: true,
  locale: process.env.PRM_BROWSER_LOCALE || "ko-KR",
  timezone: process.env.PRM_BROWSER_TIMEZONE || "Asia/Seoul",
  viewport: { width: 1600, height: 1000 },
  args: [
    `--remote-debugging-port=${cdpPort}`,
    "--remote-debugging-address=127.0.0.1",
    "--password-store=basic",
    "--gtk-version=3",
    "--disable-features=TFLiteLanguageDetectionEnabled",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",
    "--disable-gpu",
  ],
});

const page = context.pages()[0] || await context.newPage();
await page.goto(chatgptUrl).catch(() => {});

const shutdown = async () => {
  await context.close().catch(() => {});
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

setInterval(() => {}, 60_000);
