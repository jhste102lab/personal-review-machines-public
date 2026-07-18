#!/usr/bin/env node
/**
 * Long-lived CloakBrowser for personal-review-machines CDP access.
 *
 * Resource policy:
 * - Keep the browser/CDP process warm (profile + cookies) while supervised.
 * - Do NOT keep chatgpt.com rendered while idle.
 * - Reap finished/stale ChatGPT pages with page.close() — never accumulate
 *   parked about:blank tabs. Exactly one park page stays for CDP health.
 * - Review jobs open fresh pages; generation continues after send lease ends.
 */
import { pathToFileURL } from "node:url";

const userDataDir = process.env.PRM_BROWSER_PROFILE_DIR;
const cdpPort = process.env.PRM_CDP_PORT || "9222";
const cloakDir = process.env.PRM_CLOAK_DIR;
const parkUrl = process.env.PRM_PARK_URL || "about:blank";
// Default: start parked. Set PRM_BROWSER_START_URL=https://chatgpt.com/ only for debugging.
const startUrl = process.env.PRM_BROWSER_START_URL || parkUrl;
const reapIntervalMs = positiveInt(process.env.PRM_PARK_INTERVAL_MS, 30_000);
// Hard upper bound for any leftover ChatGPT tab (conversation or otherwise).
const tabMaxAgeMs = positiveInt(process.env.PRM_TAB_MAX_AGE_MS, 35 * 60 * 1000);
// After generation UI disappears, close the conversation after this grace.
const generationDoneCloseMs = positiveInt(process.env.PRM_GENERATION_DONE_CLOSE_MS, 5 * 60 * 1000);
// Root chatgpt.com pages with no generation close after this much idle time.
const rootIdleCloseMs = positiveInt(process.env.PRM_ROOT_IDLE_PARK_MS, 90_000);

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
await page.goto(startUrl).catch(() => {});

/** @type {WeakMap<object, number>} page -> monotonic ms when first observed not-generating */
const notGeneratingSince = new WeakMap();

let reapTimer = null;
const shutdown = async () => {
  if (reapTimer) {
    clearInterval(reapTimer);
  }
  await context.close().catch(() => {});
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

reapTimer = setInterval(() => {
  reapPages(context).catch((error) => {
    console.error(`[prm-cloak] reap_failed ${error?.message || error}`);
  });
}, reapIntervalMs);
if (typeof reapTimer.unref === "function") {
  reapTimer.unref();
}

// Keep event loop alive for the browser child; reap timer alone may unref.
setInterval(() => {}, 60_000);

async function reapPages(browserContext) {
  const now = Date.now();
  const live = browserContext.pages().filter((p) => !p.isClosed());
  /** @type {object[]} */
  const parkCandidates = [];

  for (const target of live) {
    if (target.isClosed()) {
      continue;
    }
    const url = safeUrl(target.url());
    if (!url || isParkedUrl(url)) {
      parkCandidates.push(target);
      continue;
    }

    if (!isChatGptHost(url.hostname)) {
      const onPageMs = await pageAgeMs(target).catch(() => 0);
      if (onPageMs >= tabMaxAgeMs) {
        console.log(`[prm-cloak] close_foreign age_ms=${Math.round(onPageMs)} url=${clipUrl(url)}`);
        await closePage(target);
      }
      continue;
    }

    const generating = await isLikelyGenerating(target).catch(() => true);
    if (generating) {
      notGeneratingSince.delete(target);
      continue;
    }

    const firstIdle = notGeneratingSince.get(target) ?? now;
    notGeneratingSince.set(target, firstIdle);
    const idleMs = now - firstIdle;
    const onPageMs = await pageAgeMs(target).catch(() => 0);
    const path = url.pathname || "/";
    const isRoot = path === "/" || path === "";
    const isConversation = /^\/c\//.test(path);

    if (isConversation) {
      if (idleMs >= generationDoneCloseMs || onPageMs >= tabMaxAgeMs) {
        console.log(
          `[prm-cloak] close_conversation idle_ms=${Math.round(idleMs)} age_ms=${Math.round(onPageMs)}`,
        );
        await closePage(target);
      }
      continue;
    }

    if (isRoot) {
      if (idleMs >= rootIdleCloseMs || onPageMs >= tabMaxAgeMs) {
        console.log(`[prm-cloak] close_root idle_ms=${Math.round(idleMs)} age_ms=${Math.round(onPageMs)}`);
        await closePage(target);
      }
      continue;
    }

    // Settings / auth / other ChatGPT surfaces.
    if (idleMs >= generationDoneCloseMs || onPageMs >= tabMaxAgeMs) {
      console.log(`[prm-cloak] close_stale path=${path} idle_ms=${Math.round(idleMs)}`);
      await closePage(target);
    }
  }

  await collapseParkPages(browserContext, parkCandidates);
}

async function collapseParkPages(browserContext, parkCandidates) {
  // Re-read live set; some parkCandidates may have been closed.
  const parked = [];
  for (const target of browserContext.pages()) {
    if (target.isClosed()) {
      continue;
    }
    const url = safeUrl(target.url());
    if (url && isParkedUrl(url)) {
      parked.push(target);
    }
  }

  // Keep exactly one park page for CDP health / connectOverCDP.
  while (parked.length > 1) {
    const extra = parked.pop();
    console.log("[prm-cloak] close_extra_park");
    await closePage(extra);
  }

  const remaining = browserContext.pages().filter((p) => !p.isClosed());
  if (remaining.length === 0) {
    const fresh = await browserContext.newPage();
    await fresh.goto(parkUrl).catch(() => {});
    console.log("[prm-cloak] ensure_park_page");
  }
}

async function closePage(target) {
  notGeneratingSince.delete(target);
  if (target.isClosed()) {
    return;
  }
  const closed = await target.close().catch(() => false);
  if (closed === false && !target.isClosed()) {
    // Last-resort navigate away so renderer is cheap if close is blocked.
    await target.goto(parkUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
  }
}

async function isLikelyGenerating(target) {
  return target.evaluate(() => {
    const visible = (el) => {
      if (!(el instanceof HTMLElement)) {
        return false;
      }
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
        return false;
      }
      const rect = el.getBoundingClientRect();
      return rect.width > 8 && rect.height > 8;
    };

    const stopSelectors = [
      '[data-testid="stop-button"]',
      'button[aria-label*="Stop" i]',
      'button[aria-label*="중지"]',
      'button[aria-label*="중단"]',
      'button[aria-label*="Stop generating" i]',
    ];
    for (const selector of stopSelectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el)) {
          return true;
        }
      }
    }

    const busySelectors = [
      '[data-testid="composer-speech-button"][aria-disabled="true"]',
      ".result-streaming",
      '[class*="result-streaming"]',
      'button[aria-label*="Stop streaming" i]',
    ];
    for (const selector of busySelectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el)) {
          return true;
        }
      }
    }

    return false;
  });
}

async function pageAgeMs(target) {
  return target.evaluate(() => performance.now());
}

function isChatGptHost(hostname) {
  const host = String(hostname || "").toLowerCase();
  return host === "chatgpt.com" || host.endsWith(".chatgpt.com")
    || host === "chat.openai.com" || host.endsWith(".openai.com");
}

function isParkedUrl(url) {
  return url.protocol === "about:" || url.href === "about:blank"
    || url.protocol === "chrome:" || url.protocol === "chrome-error:"
    || url.protocol === "devtools:";
}

function safeUrl(raw) {
  try {
    return new URL(String(raw || ""));
  } catch {
    return null;
  }
}

function clipUrl(url) {
  try {
    return `${url.origin}${url.pathname}`.slice(0, 120);
  } catch {
    return "";
  }
}

function positiveInt(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) {
    return fallback;
  }
  return Math.floor(n);
}
