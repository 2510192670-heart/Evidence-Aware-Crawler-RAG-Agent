/** Presentation only: never replaces values in API responses or artifacts. */
export function displayValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return '未记录'
  if (Array.isArray(value) && value.length === 0) return '无记录'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export function jsonLeaf(value: unknown, mode: 'summary' | 'raw'): string {
  if (mode === 'raw') return JSON.stringify(value) ?? 'undefined'
  return typeof value === 'string' && value !== '' ? JSON.stringify(value) : displayValue(value)
}
