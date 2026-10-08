// Pure SVG drawings of the AICQ look (Michael's prototype): hexagon avatars,
// cards with status pills, headings. Desktop draws `Svg` as an image, so
// every press stays on a native Button beside the drawing.

export const FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Inter, Helvetica, Arial, sans-serif"

const DARK = {
  bg: '#1c1c20',
  card: '#232328',
  cardBorder: '#34343c',
  selected: '#2b2846',
  selectedBorder: '#6d5fe0',
  text: '#f1f1f4',
  body: '#b9b9c2',
  dim: '#8c8c96',
  pillBorder: '#4a4a54',
  pillText: '#d9d9df',
  accent: '#10a37f',
  amber: '#e8b04b',
  amberBorder: '#6b5426',
  mineFill: '#1f3b31',
  mineStroke: '#2c5547',
  mineInk: '#86d9b6',
  friendFill: '#2e2a4d',
  friendStroke: '#433d6e',
  friendInk: '#bdb2ff',
}

const LIGHT: typeof DARK = {
  bg: '#ffffff',
  card: '#ffffff',
  cardBorder: '#e3e3e8',
  selected: '#f1efff',
  selectedBorder: '#7b6cf0',
  text: '#16161a',
  body: '#4a4a55',
  dim: '#7a7a85',
  pillBorder: '#d4d4dc',
  pillText: '#3a3a44',
  accent: '#0e8c6d',
  amber: '#a8670a',
  amberBorder: '#efcf95',
  mineFill: '#e2f4ec',
  mineStroke: '#b9e2d0',
  mineInk: '#18795a',
  friendFill: '#ebe8ff',
  friendStroke: '#cfc8ff',
  friendInk: '#5b4bd6',
}

/** The palette drawings use; set per render from the person's theme. */
export let C = DARK

export function usePalette(theme: string | undefined): void {
  C = theme && /light/i.test(theme) ? LIGHT : DARK
}

export function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/** Greedy wrap by an average glyph width; ellipsis past `maxLines`. */
export function wrap(text: string, px: number, size: number, maxLines: number): string[] {
  const perLine = Math.max(8, Math.floor(px / (size * 0.54)))
  const words = text.replace(/\s+/g, ' ').trim().split(' ')
  const lines: string[] = []
  let cur = ''
  for (const w of words) {
    if (!cur) cur = w
    else if ((cur + ' ' + w).length <= perLine) cur += ' ' + w
    else {
      lines.push(cur)
      cur = w
    }
    if (lines.length === maxLines) break
  }
  if (lines.length < maxLines && cur) lines.push(cur)
  const used = lines.join(' ').length
  if (used < text.replace(/\s+/g, ' ').trim().length && lines.length) {
    const last = lines[lines.length - 1]!
    lines[lines.length - 1] = (last.length > perLine - 1 ? last.slice(0, perLine - 1) : last).replace(/[\s,.;:]+$/, '') + '…'
  }
  return lines.map(l => (l.length > perLine ? l.slice(0, perLine - 1) + '…' : l))
}

function hexPath(cx: number, cy: number, r: number): string {
  const pts: string[] = []
  for (let i = 0; i < 6; i += 1) {
    const a = (Math.PI / 3) * i - Math.PI / 2
    pts.push(`${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`)
  }
  return `M${pts.join('L')}Z`
}

export function hexAvatar(cx: number, cy: number, r: number, label: string, mine: boolean): string {
  const letter = esc((label.trim()[0] ?? '?').toUpperCase())
  return `<path d="${hexPath(cx, cy, r)}" fill="${mine ? C.mineFill : C.friendFill}" stroke="${mine ? C.mineStroke : C.friendStroke}" stroke-width="1"/>`
    + `<text x="${cx}" y="${cy + r * 0.36}" text-anchor="middle" font-family="${FONT}" font-size="${(r * 0.95).toFixed(1)}" font-weight="600" fill="${mine ? C.mineInk : C.friendInk}">${letter}</text>`
}

/** A rounded status pill whose right edge sits at `right`; returns markup and width. */
export function pill(right: number, y: number, label: string, tone: 'neutral' | 'amber' | 'accent' = 'neutral'): string {
  const size = 11
  const w = Math.round(label.length * size * 0.56 + 18)
  const color = tone === 'amber' ? C.amber : tone === 'accent' ? C.accent : C.pillText
  const border = tone === 'amber' ? C.amberBorder : tone === 'accent' ? C.accent : C.pillBorder
  return `<rect x="${right - w}" y="${y}" width="${w}" height="22" rx="11" fill="none" stroke="${border}"/>`
    + `<text x="${right - w / 2}" y="${y + 15}" text-anchor="middle" font-family="${FONT}" font-size="${size}" fill="${color}">${esc(label)}</text>`
}

function svg(width: number, height: number, inner: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${inner}</svg>`
}

export type CardData = {
  name: string
  mine: boolean
  pillLabel: string
  pillTone: 'neutral' | 'amber' | 'accent'
  title: string
  narrative: string
  footer: string
}

export const CARD_W = 420

export function cardSvg(d: CardData, width = CARD_W): string {
  const pad = 20
  const lines = wrap(d.narrative, width - pad * 2, 13.5, 3)
  const titleLines = wrap(d.title, width - pad * 2, 17, 2)
  let y = 64
  const title = titleLines.map(l => { const t = `<text x="${pad}" y="${y}" font-family="${FONT}" font-size="17" font-weight="650" fill="${C.text}">${esc(l)}</text>`; y += 23; return t }).join('')
  y += 2
  const body = lines.map(l => { const t = `<text x="${pad}" y="${y}" font-family="${FONT}" font-size="13.5" fill="${C.body}">${esc(l)}</text>`; y += 20; return t }).join('')
  y += 10
  const footer = `<text x="${pad}" y="${y}" font-family="${FONT}" font-size="12" fill="${C.dim}">${esc(d.footer)}</text>`
  const h = y + 18
  return svg(width, h,
    `<rect x="0.5" y="0.5" width="${width - 1}" height="${h - 1}" rx="14" fill="${C.card}" stroke="${C.cardBorder}"/>`
    + hexAvatar(pad + 14, 30, 15, d.name, d.mine)
    + `<text x="${pad + 38}" y="35" font-family="${FONT}" font-size="14" fill="${C.text}">${esc(d.name.length > 30 ? d.name.slice(0, 29) + '…' : d.name)}</text>`
    + pill(width - pad, 19, d.pillLabel, d.pillTone)
    + title + body + footer)
}

export function headingSvg(width: number, eyebrow: string, title: string, sub: string): string {
  const subLines = wrap(sub, width - 4, 14, 2)
  let y = 74
  const subs = subLines.map(l => { const t = `<text x="0" y="${y}" font-family="${FONT}" font-size="14" fill="${C.body}">${esc(l)}</text>`; y += 20; return t }).join('')
  return svg(width, y + 4,
    `<text x="0" y="16" font-family="${FONT}" font-size="11" letter-spacing="1.4" font-weight="600" fill="${C.dim}">${esc(eyebrow.toUpperCase())}</text>`
    + `<text x="0" y="50" font-family="${FONT}" font-size="27" font-weight="700" fill="${C.text}">${esc(title)}</text>` + subs)
}

export function sectionSvg(width: number, label: string): string {
  return svg(width, 30, `<text x="0" y="22" font-family="${FONT}" font-size="17" font-weight="650" fill="${C.text}">${esc(label)}</text>`)
}

export const SIDE_W = 250

export function sideItemSvg(d: { name: string; line2: string; line3: string; mine: boolean; selected: boolean }): string {
  const h = 64
  const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)
  return svg(SIDE_W, h,
    (d.selected ? `<rect x="0.5" y="0.5" width="${SIDE_W - 1}" height="${h - 1}" rx="10" fill="${C.selected}" stroke="${C.selectedBorder}"/>` : '')
    + hexAvatar(24, 32, 13, d.name, d.mine)
    + `<text x="46" y="22" font-family="${FONT}" font-size="13.5" font-weight="600" fill="${C.text}">${esc(clip(d.name, 26))}</text>`
    + `<text x="46" y="39" font-family="${FONT}" font-size="12" fill="${C.body}">${esc(clip(d.line2, 30))}</text>`
    + `<text x="46" y="54" font-family="${FONT}" font-size="11" fill="${C.dim}">${esc(clip(d.line3, 34))}</text>`)
}

export function sideLabelSvg(label: string): string {
  return svg(SIDE_W, 26, `<text x="4" y="18" font-family="${FONT}" font-size="11" letter-spacing="1.4" font-weight="600" fill="${C.dim}">${esc(label.toUpperCase())}</text>`)
}

export function brandSvg(): string {
  return svg(120, 30,
    `<circle cx="13" cy="15" r="10" fill="none" stroke="${C.accent}" stroke-width="2.4"/><circle cx="13" cy="15" r="3.6" fill="${C.accent}"/>`
    + `<text x="32" y="21" font-family="${FONT}" font-size="17" font-weight="700" fill="${C.text}">AICQ</text>`)
}

export function detailHeaderSvg(width: number, d: { name: string; subtitle: string; reply: string; mine: boolean; pillLabel: string; pillTone: 'neutral' | 'amber' | 'accent'; title: string; narrative: string; footer: string }): string {
  const lines = wrap(d.narrative, width - 4, 15, 4)
  const titleLines = wrap(d.title, width - 4, 26, 2)
  let y = 150
  const title = titleLines.map(l => { const t = `<text x="0" y="${y}" font-family="${FONT}" font-size="26" font-weight="700" fill="${C.text}">${esc(l)}</text>`; y += 32; return t }).join('')
  const body = lines.map(l => { const t = `<text x="0" y="${y}" font-family="${FONT}" font-size="15" fill="${C.body}">${esc(l)}</text>`; y += 22; return t }).join('')
  y += 8
  return svg(width, y + 6,
    hexAvatar(20, 26, 19, d.name, d.mine)
    + `<text x="50" y="22" font-family="${FONT}" font-size="18" font-weight="650" fill="${C.text}">${esc(d.name)}</text>`
    + `<text x="50" y="41" font-family="${FONT}" font-size="12.5" fill="${C.dim}">${esc(d.subtitle)}</text>`
    + `<text x="50" y="58" font-family="${FONT}" font-size="12.5" fill="${C.dim}">${esc(d.reply)}</text>`
    + `<line x1="0" y1="78" x2="${width}" y2="78" stroke="${C.cardBorder}"/>`
    + pill(Math.round(d.pillLabel.length * 11 * 0.56 + 18), 94, d.pillLabel, d.pillTone)
    + title + body
    + `<text x="0" y="${y}" font-family="${FONT}" font-size="12.5" fill="${C.dim}">${esc(d.footer)}</text>`)
}

export function noteCardSvg(width: number, eyebrow: string, heading: string, body: string, tone: 'neutral' | 'amber'): string {
  const pad = 18
  const hl = wrap(heading, width - pad * 2, 17, 2)
  const bl = wrap(body, width - pad * 2, 14, 8)
  let y = 62
  const head = hl.map(l => { const t = `<text x="${pad}" y="${y}" font-family="${FONT}" font-size="17" font-weight="650" fill="${C.text}">${esc(l)}</text>`; y += 23; return t }).join('')
  y += hl.length ? 4 : 0
  const txt = bl.map(l => { const t = `<text x="${pad}" y="${y}" font-family="${FONT}" font-size="14" fill="${C.body}">${esc(l)}</text>`; y += 21; return t }).join('')
  const h = y + 8
  return svg(width, h,
    `<rect x="0.5" y="0.5" width="${width - 1}" height="${h - 1}" rx="12" fill="${C.card}" stroke="${tone === 'amber' ? C.amberBorder : C.cardBorder}"/>`
    + `<text x="${pad}" y="32" font-family="${FONT}" font-size="11" letter-spacing="1.3" font-weight="600" fill="${tone === 'amber' ? C.amber : C.dim}">${esc(eyebrow.toUpperCase())}</text>`
    + head + txt)
}

/** A standalone hexagon avatar, for rows whose text is native. */
export function avatarSvg(label: string, mine: boolean, size = 30): string {
  const r = size / 2 - 1
  return svg(size, size, hexAvatar(size / 2, size / 2, r, label, mine))
}
