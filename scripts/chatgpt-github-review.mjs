#!/usr/bin/env node
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const args = parseArgs(process.argv.slice(2));
const prompt = fs.readFileSync(requiredArg(args, "prompt-file"), "utf8");
const chatgptUrl = args.url || process.env.PRM_CHATGPT_URL || "https://chatgpt.com/";
const cdpUrl = args.cdp || process.env.PRM_CDP_URL || `http://127.0.0.1:${process.env.PRM_CDP_PORT || "9222"}`;
const modelName = args.model || "ChatGPT Pro Extended";
const reasoningLevel = args["reasoning-level"] || "Pro";
const sessionConfirmationTimeoutMs = 90_000;
// Soft hint only; cloak-launch reaper is the source of truth for tab close.
const tabCloseDelayMs = positiveInt(process.env.PRM_TAB_CLOSE_DELAY_MS, 30 * 60 * 1000);
// Hard cap on non-park pages in this browser while a new review tab is opened.
const maxGeneratingTabs = positiveInt(process.env.PRM_MAX_GENERATING_TABS_PER_SLOT, 4);
const generatingWaitMs = positiveInt(process.env.PRM_GENERATING_WAIT_MS, 30 * 60 * 1000);
const cloakDir = process.env.PRM_CLOAK_DIR;

if (!cloakDir) {
  throw new Error("PRM_CLOAK_DIR is required");
}

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

let browser;
let promptSubmitAttempted = false;
try {
  browser = await chromium.connectOverCDP(cdpUrl);
} catch (error) {
  // Exit 75 is reserved for a pre-send CDP connection failure. The worker
  // may retry this safely because no conversation or prompt exists yet.
  console.error(error);
  process.exitCode = 75;
}

if (browser) {
  let page;
  try {
    const context = browser.contexts()[0] || await browser.newContext();
    await cleanupBrokenPages(context);
    await collapseExtraParkPages(context);
    await waitForGenerationCapacity(context);
    page = await openChatPage(context, chatgptUrl);
    const sessionUrl = await sendPromptWithGithub(page, prompt, reasoningLevel);
    console.log(JSON.stringify({
      ok: true,
      phase: "chat-session-created",
      model: modelName,
      reasoningLevel,
      sessionUrl,
      cdpUrl,
    }));
    // Keep the tab open so server-side generation can finish.
    // cloak-launch reaper closes after generation settles; this is a backup.
    await page.evaluate((delay) => setTimeout(() => window.close(), delay), tabCloseDelayMs).catch(() => {});
    process.exit(0);
  } catch (error) {
    // A failure after clicking Send is delivery-uncertain: ChatGPT may keep
    // running server-side and publish the GitHub review after this process
    // exits. The worker uses a distinct exit code to avoid duplicate retries.
    console.error(error);
    if (!promptSubmitAttempted && page && !page.isClosed()) {
      await page.close().catch(() => {});
    }
    process.exit(promptSubmitAttempted ? 76 : 77);
  } finally {
    // Do not close browser — CDP connection is dropped on process exit.
    // Successful send leaves the page open for generation.
  }
}

function parseArgs(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) {
      continue;
    }
    const key = item.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) {
      parsed[key] = "1";
    } else {
      parsed[key] = next;
      index += 1;
    }
  }
  return parsed;
}

function requiredArg(parsed, name) {
  if (!parsed[name]) {
    throw new Error(`--${name} is required`);
  }
  return parsed[name];
}

function positiveInt(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) {
    return fallback;
  }
  return Math.floor(n);
}

function isParkedUrlString(url) {
  const raw = String(url || "");
  return raw === "" || raw === "about:blank" || raw.startsWith("about:")
    || raw.startsWith("chrome:") || raw.startsWith("chrome-error:")
    || raw.startsWith("devtools:");
}

function countBusyPages(context) {
  let count = 0;
  for (const target of context.pages()) {
    if (target.isClosed()) {
      continue;
    }
    if (!isParkedUrlString(target.url())) {
      count += 1;
    }
  }
  return count;
}

async function collapseExtraParkPages(context) {
  const parked = [];
  for (const target of context.pages()) {
    if (target.isClosed()) {
      continue;
    }
    if (isParkedUrlString(target.url())) {
      parked.push(target);
    }
  }
  while (parked.length > 1) {
    const extra = parked.pop();
    await extra.close().catch(() => {});
  }
  if (context.pages().filter((item) => !item.isClosed()).length === 0) {
    await context.newPage().catch(() => {});
  }
}

async function waitForGenerationCapacity(context) {
  const deadline = Date.now() + generatingWaitMs;
  while (Date.now() < deadline) {
    await collapseExtraParkPages(context);
    const busy = countBusyPages(context);
    if (busy < maxGeneratingTabs) {
      return;
    }
    console.error(JSON.stringify({
      phase: "wait_generation_capacity",
      busy,
      maxGeneratingTabs,
      cdpUrl,
    }));
    await new Promise((resolve) => setTimeout(resolve, 5_000));
  }
  throw new Error(
    `ChatGPT generation capacity wait timed out busy>=${maxGeneratingTabs} cdp=${cdpUrl}`,
  );
}

async function cleanupBrokenPages(context) {
  const pages = context.pages();
  for (const target of pages) {
    if (target.isClosed()) {
      continue;
    }
    const url = target.url();
    const broken =
      url.startsWith("chrome-error://")
      || url.startsWith("chrome-untrusted://")
      || url.includes("error-page")
      || /HTTP ERROR\s+(403|429|431)/i.test(await target.locator("body").innerText().catch(() => ""));
    if (!broken) {
      continue;
    }
    console.error(JSON.stringify({ phase: "cleanup_broken_page", url }));
    await target.close().catch(() => {});
  }
  // Keep at least one page alive for CDP health probes.
  if (context.pages().filter((item) => !item.isClosed()).length === 0) {
    await context.newPage().catch(() => {});
  }
}

async function openChatPage(context, url) {
  // Every review gets its own page so parallel jobs cannot navigate or type
  // into one another's conversation.
  const page = await context.newPage();
  await page.bringToFront();
  try {
    await startNewChat(page, url);
    await page.waitForTimeout(2500);
    await page.keyboard.press("Escape").catch(() => {});
    return page;
  } catch (error) {
    await page.close().catch(() => {});
    throw error;
  }
}

async function startNewChat(page, url) {
  const newChatUrl = new URL(url);
  newChatUrl.pathname = "/";
  newChatUrl.search = "";
  newChatUrl.hash = "";
  const response = await page.goto(newChatUrl.toString(), {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  const status = response ? response.status() : 0;
  if (status >= 400) {
    const body = (await page.locator("body").innerText().catch(() => "")).slice(0, 200);
    throw new Error(`ChatGPT navigation failed status=${status} body=${body}`);
  }
  await page.locator("#prompt-textarea").first().waitFor({ timeout: 20_000 });
  const existingMessages = await page.locator('[data-message-author-role]').count();
  if (existingMessages > 0) {
    throw new Error("ChatGPT did not open a fresh conversation");
  }
}

async function sendPromptWithGithub(page, message, level) {
  const textbox = page.locator("#prompt-textarea").first();
  await textbox.waitFor({ timeout: 20_000 });
  await clearComposer(page, textbox);
  await enableTemporaryChat(page);
  await attachGithubPlugin(page);
  await selectReasoningLevel(page, level);
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(300);
  await textbox.click({ timeout: 5000 });
  await page.keyboard.press("End").catch(() => {});
  await page.keyboard.insertText(message);
  await page.waitForTimeout(700);
  // The click can submit the prompt even if Playwright reports a navigation
  // or confirmation failure afterwards.
  promptSubmitAttempted = true;
  await page.locator("#composer-submit-button").click({ timeout: 10_000 });
  return confirmChatSession(page);
}

async function enableTemporaryChat(page) {
  const enabled = page.getByRole("button", {
    name: /임시 채팅 끄기|turn off temporary chat/i,
  }).last();
  if (await enabled.count()) {
    return;
  }

  const disabled = page.getByRole("button", {
    name: /임시 채팅 켜기|turn on temporary chat|enable temporary chat/i,
  }).last();
  await disabled.click({ timeout: 5000 });
  await enabled.waitFor({ timeout: 5000 });
}

async function confirmChatSession(page) {
  const signals = [
    page.waitForURL(
      (url) => /^\/c\/[^/]+$/.test(url.pathname),
      { timeout: sessionConfirmationTimeoutMs },
    ).then(() => "url_changed"),
    page.locator('[data-message-author-role="user"]').last()
      .waitFor({ state: "visible", timeout: sessionConfirmationTimeoutMs })
      .then(() => "user_message"),
  ];
  await Promise.race(signals).catch(() => { throw new Error("ChatGPT session confirmation timed out"); });
  return page.url();
}

async function selectReasoningLevel(page, level) {
  if (!level) {
    return;
  }
  const pill = page.locator("button.__composer-pill").last();
  await pill.waitFor({ timeout: 10_000 });
  const current = (await pill.innerText().catch(() => "")).trim();
  if (current === level) {
    return;
  }

  await pill.click({ timeout: 5000 });
  await page.waitForTimeout(600);
  const option = page.getByText(level, { exact: true }).last();
  await option.click({ timeout: 5000 });
  await page.waitForTimeout(700);
}

async function clearComposer(page, textbox) {
  await textbox.click({ timeout: 5000 });
  await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  await page.keyboard.press("Backspace");
  await page.waitForTimeout(300);
}

async function attachGithubPlugin(page) {
  await page.locator("#composer-plus-btn").click({ timeout: 5000 }).catch(async () => {
    await page.getByRole("button", { name: "파일 등 추가" }).click({ timeout: 5000 });
  });
  await page.waitForTimeout(700);

  const githubItems = await page.getByText("GitHub", { exact: true }).all();
  for (let index = githubItems.length - 1; index >= 0; index -= 1) {
    const box = await githubItems[index].boundingBox().catch(() => null);
    if (box && box.x > 300 && box.y > 200) {
      await githubItems[index].click({ timeout: 5000 });
      await page.waitForTimeout(700);
      return;
    }
  }
  await page.getByText("GitHub", { exact: true }).last().click({ timeout: 5000 });
  await page.waitForTimeout(700);
}
