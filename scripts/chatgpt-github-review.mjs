#!/usr/bin/env node
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const args = parseArgs(process.argv.slice(2));
const prompt = fs.readFileSync(requiredArg(args, "prompt-file"), "utf8");
const chatgptUrl = args.url || process.env.PRM_CHATGPT_URL || "https://chatgpt.com/";
const cdpUrl = args.cdp || process.env.PRM_CDP_URL || `http://127.0.0.1:${process.env.PRM_CDP_PORT || "9222"}`;
const modelName = args.model || "ChatGPT Pro Extended";
const reasoningLevel = args["reasoning-level"] || "Pro";
const sessionConfirmationTimeoutMs = 20_000;
const cloakDir = process.env.PRM_CLOAK_DIR;

if (!cloakDir) {
  throw new Error("PRM_CLOAK_DIR is required");
}

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

let browser;
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
    page = await openChatPage(context, chatgptUrl);
    const sessionUrl = await sendPromptWithGithub(page, prompt, reasoningLevel);
    console.log(JSON.stringify({
      ok: true,
      phase: "chat-session-created",
      model: modelName,
      reasoningLevel,
      sessionUrl,
    }));
  } finally {
    // The prompt continues server-side; retaining the dedicated tab only leaks
    // one renderer per review and eventually exhausts host CPU and memory.
    if (page) {
      await page.close().catch(() => {});
    }
    await browser.close().catch(() => {});
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

async function openChatPage(context, url) {
  // Every review gets its own page so parallel jobs cannot navigate or type
  // into one another's conversation.
  const page = await context.newPage();
  await page.bringToFront();
  await startNewChat(page, url);
  await page.waitForTimeout(2500);
  await page.keyboard.press("Escape").catch(() => {});
  return page;
}

async function startNewChat(page, url) {
  const newChatUrl = new URL(url);
  newChatUrl.pathname = "/";
  newChatUrl.search = "";
  newChatUrl.hash = "";
  await page.goto(newChatUrl.toString(), { waitUntil: "domcontentloaded" }).catch(() => {});
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
  await attachGithubPlugin(page);
  await selectReasoningLevel(page, level);
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(300);
  await textbox.click({ timeout: 5000 });
  await page.keyboard.press("End").catch(() => {});
  await page.keyboard.insertText(message);
  await page.waitForTimeout(700);
  await page.locator("#composer-submit-button").click({ timeout: 10_000 });
  return confirmChatSession(page);
}

async function confirmChatSession(page) {
  await page.waitForURL(
    (url) => /^\/c\/[^/]+$/.test(url.pathname),
    { timeout: sessionConfirmationTimeoutMs },
  );
  const userMessage = page.locator('[data-message-author-role="user"]').last();
  await userMessage.waitFor({ state: "visible", timeout: sessionConfirmationTimeoutMs });
  const submittedText = (await userMessage.innerText()).trim();
  if (!submittedText) {
    throw new Error("ChatGPT created a conversation without preserving the submitted prompt");
  }
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
