#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = parseArgs(process.argv.slice(2));
const prompt = fs.readFileSync(requiredArg(args, "prompt-file"), "utf8");
const chatgptUrl = args.url || process.env.PRM_CHATGPT_URL || "https://chatgpt.com/";
const cdpUrl = args.cdp || process.env.PRM_CDP_URL || `http://127.0.0.1:${process.env.PRM_CDP_PORT || "9222"}`;
const modelName = args.model || "ChatGPT Pro Extended";
const reasoningLevel = args["reasoning-level"] || "Pro";
const sessionConfirmationTimeoutMs = 90_000;
// Hard cap on non-park pages in this browser while a new review tab is opened.
const maxGeneratingTabs = positiveInt(process.env.PRM_MAX_GENERATING_TABS_PER_SLOT, 4);
const generatingWaitMs = positiveInt(process.env.PRM_GENERATING_WAIT_MS, 30 * 60 * 1000);
// Cold browser starts often hit Cloudflare. Wait long enough for auto-pass/reload.
const composerReadyTimeoutMs = positiveInt(process.env.PRM_COMPOSER_READY_MS, 120_000);
const challengeReloadAfterMs = positiveInt(process.env.PRM_CF_RELOAD_AFTER_MS, 18_000);
const challengeMaxReloads = positiveInt(process.env.PRM_CF_MAX_RELOADS, 4);
const cloakDir = process.env.PRM_CLOAK_DIR;
const home = process.env.PRM_HOME_OVERRIDE || process.env.HOME || "/tmp";
const failureArtifactDir = process.env.PRM_FAILURE_ARTIFACT_DIR
  || path.join(home, ".cache/personal-review-machines-chatgpt/failures");

if (!cloakDir) {
  throw new Error("PRM_CLOAK_DIR is required");
}

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

let browser;
let promptSubmitAttempted = false;
let activePage = null;
try {
  browser = await chromium.connectOverCDP(cdpUrl);
} catch (error) {
  // Exit 75 is reserved for a pre-send CDP connection failure. The worker
  // may retry this safely because no conversation or prompt exists yet.
  logPhase("cdp_connect_failed", { cdpUrl, error: String(error) });
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
    activePage = page;
    const sessionUrl = await sendPromptWithGithub(page, prompt, reasoningLevel);
    logPhase("chat_session_created", {
      model: modelName,
      reasoningLevel,
      sessionUrl,
      cdpUrl,
    });
    console.log(JSON.stringify({
      ok: true,
      phase: "chat-session-created",
      model: modelName,
      reasoningLevel,
      sessionUrl,
      cdpUrl,
    }));
    // Keep the tab open only while ChatGPT is generating. The browser reaper
    // closes it as soon as generation settles.
    process.exit(0);
  } catch (error) {
    // A failure after clicking Send is delivery-uncertain: ChatGPT may keep
    // running server-side and publish the GitHub review after this process
    // exits. The worker uses a distinct exit code to avoid duplicate retries.
    logPhase("review_failed", {
      promptSubmitAttempted,
      error: String(error),
      url: page && !page.isClosed() ? page.url() : null,
      title: page && !page.isClosed() ? await page.title().catch(() => "") : null,
    });
    console.error(error);
    await saveFailureArtifacts(page, error).catch(() => {});
    if (!promptSubmitAttempted && page && !page.isClosed()) {
      await page.close().catch(() => {});
    }
    process.exit(promptSubmitAttempted ? 76 : 77);
  } finally {
    // Do not close browser — CDP connection is dropped on process exit.
    // Successful send leaves the page open for generation.
    activePage = null;
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

function logPhase(phase, extra = {}) {
  console.error(JSON.stringify({
    t: new Date().toISOString(),
    phase,
    ...extra,
  }));
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
    logPhase("wait_generation_capacity", {
      busy,
      maxGeneratingTabs,
      cdpUrl,
    });
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
    logPhase("cleanup_broken_page", { url });
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
    await page.waitForTimeout(1500);
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
  logPhase("navigate_start", { url: newChatUrl.toString() });
  const response = await page.goto(newChatUrl.toString(), {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  const status = response ? response.status() : 0;
  const title = await page.title().catch(() => "");
  logPhase("navigate_done", { status, title, url: page.url() });

  // Cloudflare interstitial often returns HTTP 403 with a challenge page.
  // That is not a hard navigation failure — wait for the composer instead.
  if (status >= 400 && !isCloudflareChallenge(page, title, status)) {
    const body = (await page.locator("body").innerText().catch(() => "")).slice(0, 200);
    throw new Error(`ChatGPT navigation failed status=${status} body=${body}`);
  }

  await waitForComposerReady(page);
  const existingMessages = await page.locator('[data-message-author-role]').count();
  if (existingMessages > 0) {
    throw new Error("ChatGPT did not open a fresh conversation");
  }
  logPhase("composer_ready", { url: page.url(), title: await page.title().catch(() => "") });
}

function isCloudflareChallenge(page, title, status) {
  const pageTitle = String(title || "");
  const href = page.url();
  if (status === 403) {
    return true;
  }
  if (/잠시만 기다리|just a moment|attention required|cloudflare/i.test(pageTitle)) {
    return true;
  }
  if (/__cf_chl|cf-browser-verification|challenges\.cloudflare/i.test(href)) {
    return true;
  }
  return false;
}

async function waitForComposerReady(page) {
  const deadline = Date.now() + composerReadyTimeoutMs;
  let reloads = 0;
  let lastChallengeClickAt = 0;
  const started = Date.now();

  while (Date.now() < deadline) {
    const textarea = page.locator("#prompt-textarea").first();
    if (await textarea.count()) {
      try {
        await textarea.waitFor({ state: "visible", timeout: 3_000 });
        return;
      } catch {
        // Fall through and keep waiting.
      }
    }

    const title = await page.title().catch(() => "");
    const statusHint = {
      elapsedMs: Date.now() - started,
      title,
      url: page.url(),
      reloads,
    };
    if ((Date.now() - started) % 10_000 < 2_100) {
      logPhase("composer_wait", statusHint);
    }

    if (isCloudflareChallenge(page, title, 0) || await pageLooksLikeChallenge(page)) {
      if (Date.now() - lastChallengeClickAt > 4_000) {
        const clicked = await tryPassCloudflareChallenge(page);
        lastChallengeClickAt = Date.now();
        if (clicked) {
          logPhase("cloudflare_interact", { clicked, ...statusHint });
        }
      }
      if (
        Date.now() - started >= challengeReloadAfterMs * (reloads + 1)
        && reloads < challengeMaxReloads
      ) {
        reloads += 1;
        logPhase("cloudflare_reload", { reloads, ...statusHint });
        await page.reload({ waitUntil: "domcontentloaded", timeout: 60_000 }).catch(() => {});
      }
    }

    await page.waitForTimeout(2_000);
  }

  const title = await page.title().catch(() => "");
  const body = (await page.locator("body").innerText().catch(() => "")).slice(0, 240);
  throw new Error(
    `ChatGPT composer not ready after ${composerReadyTimeoutMs}ms title=${title} body=${body}`,
  );
}

async function pageLooksLikeChallenge(page) {
  const text = (await page.locator("body").innerText().catch(() => "")).slice(0, 500);
  if (/사람인지 확인|Verify you are human|needs to review the security/i.test(text)) {
    return true;
  }
  // Empty body + chatgpt host after cold start is often still a CF shell.
  if (!text.trim() && /chatgpt\.com/i.test(page.url())) {
    const hasComposer = await page.locator("#prompt-textarea").count();
    return hasComposer === 0;
  }
  return false;
}

async function tryPassCloudflareChallenge(page) {
  let clicked = false;

  // Visible page labels (some CF shells render outside the iframe).
  for (const pattern of [/사람인지 확인/i, /Verify you are human/i]) {
    const label = page.getByText(pattern).first();
    if (await label.count()) {
      await label.click({ timeout: 2_000 }).catch(() => {});
      clicked = true;
    }
  }

  for (const frame of page.frames()) {
    const frameUrl = frame.url();
    const isChallengeFrame = /challenges\.cloudflare|turnstile|cf-chl/i.test(frameUrl);
    try {
      const result = await frame.evaluate(() => {
        const selectors = [
          'input[type="checkbox"]',
          '[role="checkbox"]',
          "label",
          "#challenge-stage input",
          ".ctp-checkbox-label",
        ];
        for (const sel of selectors) {
          const nodes = Array.from(document.querySelectorAll(sel));
          for (const el of nodes) {
            const meta = `${el.getAttribute("aria-label") || ""} ${el.innerText || ""} ${el.id || ""}`;
            if (/사람|human|verify|confirm|checkbox|cf-/i.test(meta) || el.type === "checkbox" || sel.includes("checkbox")) {
              el.click();
              return { clicked: true, sel, meta: meta.slice(0, 80) };
            }
          }
        }
        // Last resort: click near the widget center.
        const widget = document.querySelector("#challenge-stage, .main-content, body");
        if (widget) {
          const rect = widget.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) {
            const x = rect.left + Math.min(40, rect.width / 2);
            const y = rect.top + Math.min(40, rect.height / 2);
            const target = document.elementFromPoint(x, y) || widget;
            target.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: x, clientY: y }));
            return { clicked: true, sel: "elementFromPoint", meta: "" };
          }
        }
        return { clicked: false };
      });
      if (result?.clicked) {
        clicked = true;
        logPhase("cloudflare_frame_click", { frameUrl, ...result });
      } else if (isChallengeFrame) {
        // Humanized click via Playwright on frame body.
        const body = frame.locator("body");
        if (await body.count()) {
          const box = await body.boundingBox().catch(() => null);
          if (box) {
            await page.mouse.click(box.x + Math.min(36, box.width / 2), box.y + Math.min(36, box.height / 2));
            clicked = true;
            logPhase("cloudflare_mouse_click", { frameUrl, box });
          }
        }
      }
    } catch {
      // Cross-origin frame evaluation can fail; mouse path above is the fallback.
    }
  }

  return clicked;
}

async function saveFailureArtifacts(page, error) {
  fs.mkdirSync(failureArtifactDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const base = path.join(failureArtifactDir, `fail-${stamp}`);
  const meta = {
    at: new Date().toISOString(),
    error: String(error),
    stack: error?.stack || null,
    cdpUrl,
    url: page && !page.isClosed() ? page.url() : null,
    title: page && !page.isClosed() ? await page.title().catch(() => null) : null,
    promptSubmitAttempted,
  };
  fs.writeFileSync(`${base}.json`, `${JSON.stringify(meta, null, 2)}\n`);
  if (page && !page.isClosed()) {
    await page.screenshot({ path: `${base}.png`, fullPage: true }).catch(() => {});
  }
  // Keep only recent failures to avoid unbounded disk use.
  pruneFailureArtifacts(failureArtifactDir, 30);
  logPhase("failure_artifact", { base });
}

function pruneFailureArtifacts(dir, keep) {
  let entries = [];
  try {
    entries = fs.readdirSync(dir)
      .filter((name) => name.startsWith("fail-"))
      .map((name) => ({ name, full: path.join(dir, name), mtime: fs.statSync(path.join(dir, name)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime);
  } catch {
    return;
  }
  for (const entry of entries.slice(keep * 2)) {
    fs.unlinkSync(entry.full);
  }
}

async function sendPromptWithGithub(page, message, level) {
  const textbox = page.locator("#prompt-textarea").first();
  await textbox.waitFor({ timeout: 20_000 });
  await clearComposer(page, textbox);
  await disableTemporaryChat(page);
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

async function disableTemporaryChat(page) {
  const enabled = page.getByRole("button", {
    name: /임시 채팅 끄기|turn off temporary chat/i,
  }).last();
  if (!(await enabled.count())) {
    return;
  }
  await enabled.click({ timeout: 5000 });
  await page.getByRole("button", {
    name: /임시 채팅 켜기|turn on temporary chat|enable temporary chat/i,
  }).last().waitFor({ timeout: 5000 }).catch(() => {});
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
