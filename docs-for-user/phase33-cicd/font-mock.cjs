/**
 * Offline Google Fonts responses for `next/font/google`.
 *
 * fonts.googleapis.com / fonts.gstatic.com are TLS-blocked in this environment,
 * so the production web build runs with NEXT_FONT_GOOGLE_MOCKED_RESPONSES
 * pointing here. Next.js reads this module as `mockFile[url]` for every CSS URL
 * it would have fetched, and treats a `src: url(/absolute/path)` entry as a local
 * font file to read (next/dist/compiled/@next/font/.../fetch-font-file.js).
 *
 * The woff2 payloads are genuine upstream files taken from the `geist` and
 * `@fontsource/lora` packages. This is a build-host workaround for a network
 * restriction: it supplies the font bytes the network cannot, and nothing else.
 */
const fs = require("node:fs");
const path = require("node:path");

const FONT_DIR = process.env.MO7_FONT_DIR || "/home/user/mo7-cicd/fonts";

function fontFile(...names) {
  for (const name of names) {
    const candidate = path.join(FONT_DIR, name);
    if (fs.existsSync(candidate)) return candidate;
  }
  const available = fs.existsSync(FONT_DIR)
    ? fs.readdirSync(FONT_DIR).filter((f) => f.endsWith(".woff2"))
    : [];
  throw new Error(
    `font-mock: none of ${names.join(", ")} exists in ${FONT_DIR} (have: ${available.join(", ")})`,
  );
}

const GEIST = fontFile("Geist-Variable.woff2");
const LORA = {
  400: fontFile("lora-latin-400-normal.woff2"),
  500: fontFile("lora-latin-500-normal.woff2"),
  600: fontFile("lora-latin-600-normal.woff2"),
  700: fontFile("lora-latin-600-normal.woff2"),
};

function cssFor(url) {
  const match = /family=([^:&]+)/.exec(url);
  const family = decodeURIComponent(match ? match[1] : "Geist").replace(/\+/g, " ");
  if (/lora/i.test(family)) {
    const weights = [...(url.matchAll(/(\d{3})/g) || [])].map((m) => Number(m[1]));
    const wanted = weights.length ? [...new Set(weights)] : [400];
    return wanted
      .map(
        (weight) =>
          `@font-face { font-family: '${family}'; font-style: normal; font-weight: ${weight}; font-display: swap; ` +
          `src: url(${LORA[weight] || LORA[400]}) format('woff2'); }`,
      )
      .join("\n");
  }
  return (
    `@font-face { font-family: '${family}'; font-style: normal; font-weight: 100 900; font-display: swap; ` +
    `src: url(${GEIST}) format('woff2'); }`
  );
}

module.exports = new Proxy(
  {},
  {
    get(_target, property) {
      if (typeof property !== "string" || !property.startsWith("http")) return undefined;
      return cssFor(property);
    },
    has() {
      return true;
    },
  },
);
