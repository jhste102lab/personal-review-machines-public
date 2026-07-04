#!/usr/bin/env node
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const args = parseArgs(process.argv.slice(2));
const prompt = fs.readFileSync(requiredArg(args, "prompt-file"), "utf8");
const chatgptUrl = args.url || process.env.PRM_CHATGPT_URL || "https://chatgpt.com/";
const cdpUrl = args.cdp || process.env.PRM_CDP_URL || `http://127.0.0.1:${process.env.PRM_CDP_PORT || "9222"}`;
const timeoutMs = Number(args.timeout || 3600) * 1000;
const fallbackDelayMs = Number(args["fallback-delay"] || 30) * 1000;
const modelName = args.model || "ChatGPT Pro Extended";
const reasoningLevel = args["reasoning-level"] || "Pro 확장";
const forceFallbackAfterDelay = args["force-fallback-after-delay"] === "1";
const cloakDir = process.env.PRM_CLOAK_DIR;

if (!cloakDir) {
  throw new Error("PRM_CLOAK_DIR is required");
}

const playwright = await import(
  pathToFileURL(`${cloakDir}/node_modules/playwright-core/index.js`).href
);
const { chromium } = playwright.default || playwright;

const browser = await chromium.connectOverCDP(cdpUrl);
try {
  const context = browser.contexts()[0] || await browser.newContext();
  const page = await openChatPage(context, chatgptUrl);
  await sendPromptWithGithub(page, prompt, reasoningLevel);
  const first = await waitForReviewState(page, fallbackDelayMs);
  console.log(JSON.stringify({ phase: "first-check", ...first }));

  if (forceFallbackAfterDelay || first.needsFallback) {
    if (forceFallbackAfterDelay && first.running) {
      await stopRunningResponse(page);
    }
    await sendPromptWithGithub(page, buildFallbackPrompt(prompt), reasoningLevel);
    const second = await waitForReviewState(page, Math.max(10_000, timeoutMs - fallbackDelayMs));
    console.log(JSON.stringify({ phase: "fallback-check", ...second }));
    if (second.running) {
      console.error("ChatGPT was still running after the fallback timeout.");
      process.exitCode = 124;
    } else if (second.needsFallback) {
      console.error("ChatGPT stopped without a usable review after GitHub fallback.");
      process.exitCode = 2;
    }
  } else if (first.running) {
    const remaining = Math.max(10_000, timeoutMs - fallbackDelayMs);
    const final = await waitForReviewState(page, remaining);
    console.log(JSON.stringify({ phase: "final-check", ...final }));
    if (final.running) {
      console.error("ChatGPT was still running after the review timeout.");
      process.exitCode = 124;
    } else if (final.needsFallback) {
      await sendPromptWithGithub(page, buildFallbackPrompt(prompt), reasoningLevel);
      const fallback = await waitForReviewState(page, Math.max(10_000, timeoutMs / 2));
      console.log(JSON.stringify({ phase: "late-fallback-check", ...fallback }));
      if (fallback.running) {
        console.error("ChatGPT was still running after the late fallback timeout.");
        process.exitCode = 124;
      } else if (fallback.needsFallback) {
        process.exitCode = 2;
      }
    }
  }

  console.log(JSON.stringify({ ok: true, model: modelName, reasoningLevel, url: page.url() }));
} finally {
  await browser.close().catch(() => {});
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
  const existing = context.pages().find((candidate) => candidate.url().includes("chatgpt.com"));
  const page = existing || await context.newPage();
  await page.bringToFront();
  await page.goto(url, { waitUntil: "domcontentloaded" }).catch(() => {});
  await page.waitForTimeout(2500);
  await page.keyboard.press("Escape").catch(() => {});
  return page;
}

async function sendPromptWithGithub(page, message, level) {
  const textbox = page.locator("#prompt-textarea").first();
  await textbox.waitFor({ timeout: 20_000 });
  await clearComposer(page, textbox);
  await selectReasoningLevel(page, level);
  await attachGithubPlugin(page);
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(300);
  await textbox.click({ timeout: 5000 });
  await page.keyboard.press("End").catch(() => {});
  await page.keyboard.insertText(message);
  await page.waitForTimeout(700);
  await page.locator("#composer-submit-button").click({ timeout: 10_000 });
}

async function stopRunningResponse(page) {
  const stopButton = page.locator("#composer-submit-button[aria-label='답변 중지']").first();
  if (await stopButton.count().catch(() => 0)) {
    await stopButton.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }
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

async function waitForReviewState(page, waitMs) {
  const deadline = Date.now() + waitMs;
  let last = await inspectState(page);
  while (Date.now() < deadline) {
    await page.waitForTimeout(Math.min(5000, Math.max(1000, deadline - Date.now())));
    last = await inspectState(page);
    if (!last.running && (last.hasAssistantReview || last.hasFailureText || last.hasBlankToolOnlyStop)) {
      break;
    }
  }
  return {
    ...last,
    needsFallback: !last.running && (last.hasFailureText || last.hasBlankToolOnlyStop || !last.hasAssistantReview),
  };
}

async function inspectState(page) {
  return page.evaluate(() => {
    const body = document.body.innerText || "";
    const assistantTexts = [...document.querySelectorAll('[data-message-author-role="assistant"]')]
      .map((element) => element.innerText || "")
      .filter(Boolean);
    const lastAssistant = assistantTexts.at(-1) || "";
    const running = body.includes("답변 중지") || body.includes("생각 중") || body.includes("앱 요청 실행 중");
    const hasAppResponse = body.includes("앱 응답 수신함");
    const hasFailureText =
      body.includes("PR diff를 가져오지 못했다") ||
      body.includes("변경 내용을 확인할 수 없") ||
      body.includes("진행 불가") ||
      body.includes("접근이 막혔");
    const hasReviewShape =
      /위치\s*\n|문제\s*\n|영향\s*\n|수정 방향|권장 방향|코드 리뷰 결과/.test(lastAssistant) ||
      /위치\s*\n|문제\s*\n|영향\s*\n|수정 방향|권장 방향|코드 리뷰 결과/.test(body.slice(-6000));
    const hasBlankToolOnlyStop = hasAppResponse && !running && !hasReviewShape && !hasFailureText;
    return {
      url: location.href,
      running,
      hasAppResponse,
      hasFailureText,
      hasAssistantReview: hasReviewShape,
      hasBlankToolOnlyStop,
      tail: body.slice(-1500),
    };
  });
}

function buildFallbackPrompt(originalPrompt) {
  const repo = matchLine(originalPrompt, /^Git repo:\s*(.+)$/m) || "{repo}";
  const pr = matchLine(originalPrompt, /^PR 번호:\s*#?(\d+)/m) || "{number}";
  return [
    `계속 진행하라. GitHub 앱을 사용해서 ${repo} PR #${pr}의 실제 PR diff를 다시 확인하고, 위 코드 리뷰 지시문을 그대로 적용해서 코드 리뷰 결과를 작성하라.`,
    "가능하면 GitHub PR에 직접 코멘트하고, 직접 코멘트할 수 없으면 이 채팅창에 코드 리뷰 결과만 작성하라.",
  ].join(" ");
}

function matchLine(text, pattern) {
  const match = text.match(pattern);
  return match?.[1]?.trim();
}
