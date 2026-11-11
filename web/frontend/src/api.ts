const BASE = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = body?.detail || body?.error || res.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as T
}

export type JobSnap = {
  loop_id?: string
  job_state: string
  message: string
  error: string | null
  log_tail: string[]
}

export type LoopDetail = {
  loop_id: string
  phase: string
  dataset?: string
  scenes?: string
  items?: string
  stats?: { v1?: Record<string, unknown>; v2?: Record<string, unknown> }
  decision?: Record<string, unknown> | null
  files?: Record<string, boolean>
  job: JobSnap
  diagnosis_summary?: {
    avg_score?: number
    sample_count?: number
    quality_diagnosis?: string
    key_bottleneck?: string
    systemic_issue?: string
    optimization_priority?: unknown[]
  } | null
  compare?: {
    v1?: { avg_score?: number; scored_count?: number }
    v2?: { avg_score?: number; scored_count?: number }
    delta_avg?: number
    suggestion?: string
    suggestion_reason?: string
    improved_dimensions?: string[]
    regressed_dimensions?: string[]
  } | null
  batch_gaps?: {
    active?: string
    v1?: {
      total?: number
      success?: number
      pending?: number
      pending_reply?: number
      pending_score?: number
    }
    v2?: {
      total?: number
      success?: number
      pending?: number
      pending_reply?: number
      pending_score?: number
    }
  }
}

export const api = {
  health: () => request<{ ok: boolean; config_exists: boolean }>('/api/health'),
  datasets: () => request<{ ok: boolean; data: { name: string; path: string }[] }>('/api/datasets'),
  agents: () =>
    request<{ ok: boolean; data: { id: string; name: string; pe_file: string; source: string }[] }>(
      '/api/agents',
    ),
  scorers: () =>
    request<{ ok: boolean; data: { id: string; name: string; path: string }[] }>('/api/scorers'),
  listLoops: () =>
    request<{ ok: boolean; data: { loop_id: string; phase: string; updated_at?: string }[] }>(
      '/api/loops',
    ),
  start: (body: {
    agent_id: string
    dataset: string
    scorer_id: string
    ignore_fail?: boolean
  }) =>
    request<{ ok: boolean; data: { loop_id: string; phase: string } }>('/api/loops', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getLoop: (id: string) =>
    request<{ ok: boolean; data: LoopDetail }>(`/api/loops/${id}`),
  resume: (id: string, ignore_fail = true, force_phase = '') =>
    request(`/api/loops/${id}/resume`, {
      method: 'POST',
      body: JSON.stringify({ ignore_fail, force_phase }),
    }),
  annotate: (
    id: string,
    body: { business_summary: string; adopted_indices: number[]; adopt_all: boolean },
  ) =>
    request(`/api/loops/${id}/annotate`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getPe: (id: string, which: 'v1' | 'draft' | 'v2') =>
    request<{ ok: boolean; data: { content: string } }>(`/api/loops/${id}/pe/${which}`),
  putDraft: (id: string, content: string) =>
    request(`/api/loops/${id}/pe/draft`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
  optimize: (id: string, mode: 'auto' | 'keep') =>
    request(`/api/loops/${id}/optimize`, {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),
  adoptDraft: (id: string) =>
    request(`/api/loops/${id}/adopt-draft`, {
      method: 'POST',
      body: JSON.stringify({ ignore_fail: true }),
    }),
  decide: (id: string, decision: 'ADOPTED' | 'ROLLED_BACK', note: string) =>
    request(`/api/loops/${id}/decide`, {
      method: 'POST',
      body: JSON.stringify({ decision, note, write_config: true }),
    }),
  evalPreview: (id: string, version: 'v1' | 'v2') =>
    request<{
      ok: boolean
      data: { columns: string[]; rows: Record<string, unknown>[]; exists: boolean; total?: number }
    }>(`/api/loops/${id}/eval/${version}`),
  report: (id: string, name: 'diagnosis' | 'annotation' | 'compare') =>
    request<{ ok: boolean; data: Record<string, unknown> }>(`/api/loops/${id}/reports/${name}`),
}
