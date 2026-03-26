export const THEME_FILL_COLORS = [
  '#F4B9B9',
  '#F3DAB5',
  '#F2EBB5',
  '#D2F3B4',
  '#B4F3BA',
  '#B5F2D3',
  '#B5EBF4',
  '#B6CDF3',
  '#C3B5F2',
  '#EBB8F4',
  '#F3B4DB',
  '#E2E2E2',
] as const

export const THEME_DARK_COLORS = [
  '#C79696',
  '#C9B495',
  '#C3BD90',
  '#ADC893',
  '#8EC293',
  '#89BAA2',
  '#8EBBC1',
  '#91A4C2',
  '#968CBE',
  '#B289B9',
  '#BA85A6',
  '#B6B6B6',
] as const

const LEGACY_CONTRACT_COLORS = [
  '#FD6767E6',
  '#FF956EE6',
  '#FFA500E6',
  '#80A700BC',
  '#21D22DBB',
  '#065700CA',
  '#B36EF985',
  '#1B87F3BB',
  '#87CEFAE6',
  '#ADD8E6E6',
] as const

export const CFG_EDGE_COLORS = {
  NORMAL: '#8698B2',
  JUMP: '#A99D93',
  CALL: '#769B7E',
  DELEGATECALL: '#7898BC',
  TERMINATE: '#AE877B',
} as const

const THEME_BY_FILL = new Map<string, { fill: string; dark: string }>()

for (let i = 0; i < THEME_FILL_COLORS.length; i += 1) {
  THEME_BY_FILL.set(THEME_FILL_COLORS[i].toLowerCase(), {
    fill: THEME_FILL_COLORS[i],
    dark: THEME_DARK_COLORS[i],
  })
}

for (let i = 0; i < LEGACY_CONTRACT_COLORS.length; i += 1) {
  THEME_BY_FILL.set(normalizeColor(LEGACY_CONTRACT_COLORS[i]), {
    fill: THEME_FILL_COLORS[i],
    dark: THEME_DARK_COLORS[i],
  })
}

export function normalizeColor(color: string | null | undefined): string {
  return (color || '').trim().toLowerCase().replace(/[^#a-f0-9]/g, '')
}

export function resolveThemeColor(color: string | null | undefined) {
  return THEME_BY_FILL.get(normalizeColor(color)) || null
}

export function getDarkAccentForColor(color: string | null | undefined, fallback = '#6B7280') {
  return resolveThemeColor(color)?.dark || fallback
}

export function getFillColorForColor(color: string | null | undefined, fallback = '#E2E2E2') {
  return resolveThemeColor(color)?.fill || fallback
}

export function getReadableTextOnDark(color: string | null | undefined) {
  const normalized = normalizeColor(color)
  return normalized === '#b6b6b6' ? '#1f2937' : '#ffffff'
}
