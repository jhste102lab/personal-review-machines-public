#!/usr/bin/env node
/**
 * Long-lived CloakBrowser for personal-review-machines CDP access.
 *
 * CPU policy:
 * - Keep the browser/CDP process warm (profile + cookies).
 * - Do NOT keep chatgpt.com rendered while idle.
 * - Park idle ChatGPT tabs to about:blank; review jobs open fresh pages.
 */
import { pathToFileURL } from "node:url";

const userDataDir = process.env.PRM_BROWSER_PROFILE_DIR;
const cdpPort = process.env.PRM_CDP_PORT || "9222";
const cloakDir = process.env.PRM_CLOAK_DIR;
const parkUrl = process.env.PRM_PARK_URL || "about:blank";
// Default: start parked. Set PRM_BROWSER_START_URL=https://chatgpt.com/ only for debugging.
const startUrl = process.env.PRM_BROWSER_START_URL || parkUrl;
const parkIntervalMs = positiveInt(process.env.PRM_PARK_INTERVAL_MS, 60_000);
// Conversation tabs already self-close after 30m in review; park leftovers a bit later.
const tabMaxAgeMs = positiveInt(process.env.PRM_TAB_MAX_AGE_MS, 35 * 60 * 1000);
// Root chatgpt.com pages with no generation park after this much time on-page.
const rootIdleParkMs = positiveInt(process.env.PRM_ROOT_IDLE_PARK_MS, 90_000);

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

let parkTimer = null;
const shutdown = async () => {
  if (parkTimer) {
    clearInterval(parkTimer);
  }
  await context.close().catch(() => {});
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

parkTimer = setInterval(() => {
  parkIdlePages(context).catch((error) => {
    console.error(`[prm-cloak] park_idle_failed ${error?.message || error}`);
  });
}, parkIntervalMs);
// Allow the process to exit on signals even if the timer is the only handle.
if (typeof parkTimer.unref === "function") {
  parkTimer.unref();
}

// Keep event loop alive for the browser child; park timer alone may unref.
setInterval(() => {}, 60_000);

async function parkIdlePages(browserContext) {
  const pages = browserContext.pages();
  for (const target of pages) {
    if (target.isClosed()) {
      continue;
    }
    const url = safeUrl(target.url());
    if (!url || isParkedUrl(url)) {
      continue;
    }
    if (!isChatGptHost(url.hostname)) {
      continue;
    }

    const generating = await isLikelyGenerating(target).catch(() => true);
    if (generating) {
      continue;
    }

    const onPageMs = await pageAgeMs(target).catch(() => 0);
    const path = url.pathname || "/";
    const isRoot = path === "/" || path === "";
    const isConversation = /^\/c\//.test(path);

    if (isRoot && onPageMs >= rootIdleParkMs) {
      console.log(`[prm-cloak] park_root url=${url.origin}${path}`);
      await target.goto(parkUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
      continue;
    }

    if (isConversation && onPageMs >= tabMaxAgeMs) {
      console.log(`[prm-cloak] close_stale_conversation age_ms=${Math.round(onPageMs)}`);
      const closed = await target.close().catch(() => false);
      if (closed === false) {
        await target.goto(parkUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
      }
      continue;
    }

    // Leftover non-conversation ChatGPT surfaces (settings, auth, etc.) after long idle.
    if (!isRoot && !isConversation && onPageMs >= tabMaxAgeMs) {
      console.log(`[prm-cloak] park_stale path=${path} age_ms=${Math.round(onPageMs)}`);
      await target.goto(parkUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
    }
  }

  // Probe and CDP clients expect at least one live page/context.
  if (browserContext.pages().filter((p) => !p.isClosed()).length === 0) {
    const fresh = await browserContext.newPage();
    await fresh.goto(parkUrl).catch(() => {});
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

    // Streaming / in-progress affordances ChatGPT has used over time.
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
  return target.evaluate(() => {
    // timeorigin-relative; resets on navigation — intended for per-page lifetime.
    return performance.now();
  });
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

function positiveInt(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) {
    return fallback;
  }
  return Math.floor(n);
}
