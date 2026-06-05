// chrome/src/content.ts
//
// The page-meta extractor. This is the single source of truth for the
// metadata we read out of a page's DOM.
//
// It is injected into the page by background.ts via
//   chrome.scripting.executeScript({ func: extractPageMeta, args: [url] })
// which serializes the function to source and runs it IN the page. That
// imposes a hard constraint: extractPageMeta must be SELF-CONTAINED — no
// imports, no references to any module-level binding. It may only touch:
//   * its `pageUrl` argument (passed through executeScript `args`)
//   * page globals that exist in the page context (`document`, `URL`)
//
// Because it reads the page's global `document` rather than a passed-in
// node, the unit test exercises the EXACT function injected at runtime by
// installing fixture HTML into the (jsdom) global document — so the test
// fails if this real extraction path ever drifts.

export interface PageMeta {
  title: string | null;
  og_description: string | null;
  og_type: string | null;
  favicon_url: string | null;
  lang: string | null;
}

export function extractPageMeta(pageUrl: string): PageMeta {
  // Self-contained: everything below references only `pageUrl`, `document`,
  // and `URL`. Do NOT factor any part of this out to a module-level helper
  // — that would survive bundling as an out-of-scope reference and throw
  // once injected into the page.
  const metaContent = (prop: string): string | null =>
    (document.querySelector(`meta[property="${prop}"]`) as HTMLMetaElement | null)
      ?.content?.trim() || null;

  const linkIcon = document.querySelector(
    'link[rel="icon"], link[rel="shortcut icon"]',
  ) as HTMLLinkElement | null;
  const href = linkIcon?.getAttribute("href");
  let favicon_url: string;
  try {
    favicon_url = href
      ? new URL(href, pageUrl).toString()
      : new URL("/favicon.ico", pageUrl).toString();
  } catch {
    favicon_url = new URL("/favicon.ico", pageUrl).toString();
  }

  const title = document.title?.trim() || null;
  const lang = document.documentElement.getAttribute("lang");

  return {
    title: title === "" ? null : title,
    og_description: metaContent("og:description"),
    og_type: metaContent("og:type"),
    favicon_url,
    lang: lang && lang !== "" ? lang : null,
  };
}
