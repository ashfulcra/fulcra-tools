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

// ---- the agent universe map ---------------------------------------------------

export type MapNode = { key: string; label: string; liveness: string; blocked: number; blockedOnOwner: number; peer: boolean }
export type MapColumn = { machine: string; subtitle: string; platforms: { platform: string; nodes: MapNode[] }[] }
export type MapMesh = { key: string; label: string; kind: string; members: string[] }

const LIVE_COLOR: Record<string, string> = { live: '#2fbf71', idle: '#e8b04b', stale: '#8c8c96', lapsed: '#e07b39', unknown: '#5c5c66' }
const MESH_COLOR: Record<string, string> = { workspace: '#6d8cff', v5: '#b07cff', 'cross-account': '#2fb3bf' }

/** The whole universe as one drawing; returns the markup and its size. */
const CHUNK = 14

/** Long columns split into continuation columns so the drawing stays readable. */
function chunked(cols: readonly MapColumn[]): MapColumn[] {
  const out: MapColumn[] = []
  for (const col of cols) {
    let cur: MapColumn = { ...col, platforms: [] }
    let count = 0
    for (const pf of col.platforms) {
      for (let i = 0; i < pf.nodes.length; i += 1) {
        if (count === CHUNK) {
          out.push(cur)
          cur = { machine: `${col.machine} (cont.)`, subtitle: col.subtitle, platforms: [] }
          count = 0
        }
        const last = cur.platforms[cur.platforms.length - 1]
        if (last && last.platform === pf.platform) last.nodes.push(pf.nodes[i]!)
        else cur.platforms.push({ platform: pf.platform, nodes: [pf.nodes[i]!] })
        count += 1
      }
    }
    if (cur.platforms.length || !out.length) out.push(cur)
  }
  return out
}

export function universeSvg(colsIn: readonly MapColumn[], meshes: readonly MapMesh[], owner: string, selected: string | null, maxWidth = 1200): { source: string; width: number; height: number } {
  const cols = chunked(colsIn)
  const colW = 236
  const gap = 18
  const top = 150
  const perRow = Math.max(3, Math.floor((maxWidth - gap) / (colW + gap)))
  const width = Math.max(720, Math.min(cols.length, perRow) * (colW + gap) + gap)
  const pos = new Map<string, { x: number; y: number }>()
  let parts = ''
  let maxY = top
  let rowTop = top
  let rowBottom = top
  cols.forEach((col, ci) => {
    if (ci > 0 && ci % perRow === 0) {
      rowTop = rowBottom + gap
    }
    const x0 = gap + (ci % perRow) * (colW + gap)
    let y = rowTop
    const headH = 40
    const bodyStart = y + headH
    let inner = ''
    let yy = bodyStart + 6
    for (const pf of col.platforms) {
      inner += `<text x="${x0 + 14}" y="${yy + 12}" font-family="${FONT}" font-size="10.5" letter-spacing="1.1" font-weight="600" fill="${C.dim}">${esc(pf.platform.toUpperCase())}</text>`
      yy += 20
      for (const n of pf.nodes) {
        const cy = yy + 11
        const cx = x0 + 22
        pos.set(n.key, { x: cx, y: cy })
        const sel = n.key === selected
        if (sel) inner += `<rect x="${x0 + 6}" y="${yy - 2}" width="${colW - 12}" height="26" rx="8" fill="${C.selected}" stroke="${C.selectedBorder}"/>`
        inner += n.peer
          ? `<path d="${hexPathPublic(cx, cy, 7)}" fill="${LIVE_COLOR[n.liveness] ?? LIVE_COLOR.unknown}"/>`
          : `<circle cx="${cx}" cy="${cy}" r="6.5" fill="${LIVE_COLOR[n.liveness] ?? LIVE_COLOR.unknown}"/>`
        const label = n.label.length > 24 ? n.label.slice(0, 23) + '…' : n.label
        inner += `<text x="${cx + 14}" y="${cy + 4.5}" font-family="${FONT}" font-size="12.5" fill="${C.text}">${esc(label)}</text>`
        if (n.blocked > 0) {
          const bx = x0 + colW - 30
          inner += `<rect x="${bx}" y="${cy - 9}" width="22" height="18" rx="9" fill="${n.blockedOnOwner ? '#c0392b' : '#7a4a14'}"/><text x="${bx + 11}" y="${cy + 4}" text-anchor="middle" font-family="${FONT}" font-size="10.5" font-weight="700" fill="#fff">${n.blocked}</text>`
        }
        yy += 26
      }
      yy += 6
    }
    const h = Math.max(yy - y + 6, headH + 30)
    parts += `<rect x="${x0}" y="${y}" width="${colW}" height="${h}" rx="14" fill="${C.card}" stroke="${C.cardBorder}"/>`
      + `<text x="${x0 + 14}" y="${y + 22}" font-family="${FONT}" font-size="14" font-weight="650" fill="${C.text}">${esc(col.machine)}</text>`
      + `<text x="${x0 + 14}" y="${y + 36}" font-family="${FONT}" font-size="10.5" fill="${C.dim}">${esc(col.subtitle)}</text>`
      + `<line x1="${x0}" y1="${bodyStart}" x2="${x0 + colW}" y2="${bodyStart}" stroke="${C.cardBorder}"/>` + inner
    maxY = Math.max(maxY, y + h)
    rowBottom = Math.max(rowBottom, y + h)
  })

  // Owner and mesh hubs across the top.
  const ownerX = width / 2
  const ownerY = 30
  let hubs = ''
  let edges = ''
  const hubW = 150
  const hubGap = 14
  const hubsW = meshes.length * (hubW + hubGap) - hubGap
  meshes.forEach((m, i) => {
    const hx = width / 2 - hubsW / 2 + i * (hubW + hubGap)
    const hy = 84
    const color = MESH_COLOR[m.kind] ?? C.dim
    hubs += `<rect x="${hx}" y="${hy}" width="${hubW}" height="26" rx="13" fill="${C.card}" stroke="${color}"/>`
      + `<text x="${hx + hubW / 2}" y="${hy + 17}" text-anchor="middle" font-family="${FONT}" font-size="11" fill="${color}">${esc(m.label.length > 22 ? m.label.slice(0, 21) + '…' : m.label)} · ${m.members.length}</text>`
    edges += `<line x1="${ownerX}" y1="${ownerY + 14}" x2="${hx + hubW / 2}" y2="${hy}" stroke="${color}" stroke-opacity="0.5"/>`
    for (const k of m.members) {
      const p = pos.get(k)
      if (!p) continue
      const sx = hx + hubW / 2
      const sy = hy + 26
      edges += `<path d="M${sx},${sy} C${sx},${(sy + p.y) / 2} ${p.x - 30},${p.y - 40} ${p.x - 8},${p.y}" fill="none" stroke="${color}" stroke-opacity="0.35" stroke-width="1.2"${m.kind === 'cross-account' ? ' stroke-dasharray="4 3"' : ''}/>`
    }
  })
  // Blocked-on-owner links run to the owner.
  for (const col of cols) for (const pf of col.platforms) for (const n of pf.nodes) {
    if (!n.blockedOnOwner) continue
    const p = pos.get(n.key)
    if (!p) continue
    edges += `<path d="M${p.x},${p.y - 7} C${p.x},${(ownerY + p.y) / 2} ${ownerX},${ownerY + 60} ${ownerX},${ownerY + 14}" fill="none" stroke="#c0392b" stroke-opacity="0.55" stroke-width="1.4"/>`
  }
  const ownerNode = `<circle cx="${ownerX}" cy="${ownerY}" r="15" fill="${C.selected}" stroke="${C.selectedBorder}" stroke-width="1.5"/>`
    + `<text x="${ownerX}" y="${ownerY + 5}" text-anchor="middle" font-family="${FONT}" font-size="13" font-weight="700" fill="${C.text}">${esc((owner[0] ?? 'Y').toUpperCase())}</text>`
    + `<text x="${ownerX + 22}" y="${ownerY + 5}" font-family="${FONT}" font-size="12.5" font-weight="600" fill="${C.text}">${esc(owner)}</text>`
  const legendY = maxY + 26
  const legend = Object.entries(LIVE_COLOR).map(([k, c], i) => `<circle cx="${gap + 8 + i * 84}" cy="${legendY}" r="5" fill="${c}"/><text x="${gap + 18 + i * 84}" y="${legendY + 4}" font-family="${FONT}" font-size="11" fill="${C.dim}">${k}</text>`).join('')
    + `<rect x="${gap + 430}" y="${legendY - 9}" width="20" height="18" rx="9" fill="#c0392b"/><text x="${gap + 456}" y="${legendY + 4}" font-family="${FONT}" font-size="11" fill="${C.dim}">blocked on you</text>`
    + `<rect x="${gap + 560}" y="${legendY - 9}" width="20" height="18" rx="9" fill="#7a4a14"/><text x="${gap + 586}" y="${legendY + 4}" font-family="${FONT}" font-size="11" fill="${C.dim}">blocked</text>`
  const height = legendY + 18
  return { source: svg(width, height, edges + hubs + parts + ownerNode + legend), width, height }
}

export function hexPathPublic(cx: number, cy: number, r: number): string {
  return hexPath(cx, cy, r)
}
