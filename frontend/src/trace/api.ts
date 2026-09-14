export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type Step = { key: string; state: 'reached' | 'failed' | 'pending' | 'absent'; at: string | null; duration_s: number | null; evidence: Record<string, Json> | null }
export type Trace = {
  trace_schema_version: number; task_id: string; status: string | null; phase: string;
  source: string | null; model: string | null; model_calls: number | null;
  elapsed_seconds: number | null; timeline_available: boolean; created_at: string | null;
  steps: Step[]; outcome: Record<string, Json>; repair: Record<string, Json>;
  diagnostics: { failure_retrieval: Record<string, Json> | null };
  artifacts: { filename: string; sha256: string | null; size_bytes: number | null }[];
}
export { displayValue as display } from './display'
export function object(value: Json | undefined): Record<string, Json> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {}
}
export async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch('/api/v1' + path, { method: 'GET', signal })
  if (!response.ok) {
    const messages: Record<number, string> = {404: '任务或产物不存在', 409: '产物无法读取或完整性校验失败'}
    throw new Error(`${response.status} · ${messages[response.status] ?? '读取失败，请重试'}`)
  }
  return response.json()
}
export function taskPath(id: string) { return '/tasks/' + encodeURIComponent(id) }
