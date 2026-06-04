// chrome/src/content.ts
//
// Content script. Runs inside the page (injected via
// chrome.scripting.executeScript) to read DOM metadata that's not visible
// to the service worker.
//
// Self-contained: no imports of other src/ files. crxjs builds this as a
// separate bundle that gets injected.

export interface PageMeta {
  title: string | null;
  og_description: string | null;
  og_type: string | null;
  favicon_url: string | null;
  lang: string | null;
}

function metaContent(doc: Document, prop: string): string | null {
  const el = doc.querySelector(`meta[property="${prop}"]`) as HTMLMetaElement | null;
  const c = el?.content?.trim();
  return c && c !== "" ? c : null;
}

function findFavicon(doc: Document, pageUrl: string): string {
  const candidates: HTMLLinkElement[] = Array.from(
    doc.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]'),
  ) as HTMLLinkElement[];
  const href = candidates[0]?.getAttribute("href");
  if (href && href !== "") {
    try {
      return new URL(href, pageUrl).toString();
    } catch {
      // fall through
    }
  }
  return new URL("/favicon.ico", pageUrl).toString();
}

export function extractPageMeta(doc: Document, pageUrl: string): PageMeta {
  const titleEl = doc.querySelector("title");
  const title = titleEl?.textContent?.trim() || null;
  const lang = doc.documentElement.getAttribute("lang");
  return {
    title: title === "" ? null : title,
    og_description: metaContent(doc, "og:description"),
    og_type: metaContent(doc, "og:type"),
    favicon_url: findFavicon(doc, pageUrl),
    lang: lang && lang !== "" ? lang : null,
  };
}
