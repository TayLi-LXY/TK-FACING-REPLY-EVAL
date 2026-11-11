import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type LoopDetail } from './api'
import './App.css'

const PHASES = [
  'RUNNING_V1',
  'DEEP_DIVING',
  'WAITING_ANNOTATION',
  'OPTIMIZING',
  'RUNNING_V2',
  'COMPARING',
  'DONE',
] as const

type PhaseId = (typeof PHASES)[number]

const PHASE_LABEL: Record<string, string> = {
  IDLE: '空闲',
  RUNNING_V1: 'V1 跑批',
  FAILED: '失败',
  DEEP_DIVING: '深钻诊断',
  WAITING_ANNOTATION: '人工校准',
  OPTIMIZING: 'PE 优化',
  RUNNING_V2: 'V2 跑批',
  COMPARING: '对比决策',
  DONE: '完成',
}

function phaseIndex(p: string): number {
  return PHASES.indexOf(p as PhaseId)
}

/** 实际状态机阶段 → 步进条上对应的位置（FAILED 归到 V1） */
function stepperPhase(phase: string): string {
  if (phase === 'FAILED' || phase === 'IDLE') return 'RUNNING_V1'
  return phase
}

function optLabel(item: unknown, i: number): string {
  if (item && typeof item === 'object') {
    const o = item as Record<string, unknown>
    const dim = o.dimension || o.rank || i + 1
    const problem = o.problem || ''
    const direction = o.direction || ''
    return `[${i + 1}] ${dim}: ${problem}${direction ? ` → ${direction}` : ''}`
  }
  return `[${i + 1}] ${String(item)}`
}

export default function App() {
  const [datasets, setDatasets] = useState<{ name: string; path: string }[]>([])
  const [agents, setAgents] = useState<{ id: string; name: string; pe_file: string; source: string }[]>([])
  const [scorers, setScorers] = useState<{ id: string; name: string; path: string }[]>([])
  const [loops, setLoops] = useState<{ loop_id: string; phase: string; updated_at?: string }[]>([])
  const [agentId, setAgentId] = useState('')
  const [dataset, setDataset] = useState('测评集_抽样版.xlsx')
  const [scorerId, setScorerId] = useState('default')
  const [loopId, setLoopId] = useState<string | null>(null)
  const [detail, setDetail] = useState<LoopDetail | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [configOk, setConfigOk] = useState<boolean | null>(null)

  /** 用户点选要看的阶段；null = 跟随实际当前阶段 */
  const [viewPhase, setViewPhase] = useState<string | null>(null)

  const [summary, setSummary] = useState('')
  const [selectedDirs, setSelectedDirs] = useState<number[]>([])
  const [adoptAll, setAdoptAll] = useState(true)
  const [draft, setDraft] = useState('')
  const [decideNote, setDecideNote] = useState('')
  const [annotation, setAnnotation] = useState<Record<string, unknown> | null>(null)
  const [evalPreview, setEvalPreview] = useState<{
    version: string
    columns: string[]
    rows: Record<string, unknown>[]
    total?: number
  } | null>(null)

  const loadMeta = useCallback(async () => {
    try {
      const [h, d, a, s, l] = await Promise.all([
        api.health(),
        api.datasets(),
        api.agents(),
        api.scorers(),
        api.listLoops(),
      ])
      setConfigOk(h.config_exists)
      setDatasets(d.data)
      setAgents(a.data)
      setScorers(s.data)
      setLoops(l.data)
      if (a.data[0]) setAgentId((prev) => prev || a.data[0].id)
      if (s.data[0]) {
        const preferred = s.data.find((x) => x.id === 'default') || s.data[0]
        setScorerId((prev) => (s.data.some((x) => x.id === prev) ? prev : preferred.id))
      }
      if (d.data.some((x) => x.name === '测评集_抽样版.xlsx')) {
        setDataset('测评集_抽样版.xlsx')
      } else if (d.data.some((x) => x.name === '测评集.csv')) {
        setDataset('测评集.csv')
      } else if (d.data[0]) {
        setDataset(d.data[0].name)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void loadMeta()
  }, [loadMeta])

  useEffect(() => {
    setViewPhase(null)
    setAnnotation(null)
    setEvalPreview(null)
    setSummary('')
    setSelectedDirs([])
    setAdoptAll(true)
    setDraft('')
    setDecideNote('')
  }, [loopId])

  useEffect(() => {
    if (!loopId) return
    let cancelled = false
    const tick = async () => {
      try {
        const res = await api.getLoop(loopId)
        if (cancelled) return
        setDetail(res.data)
        setError('')
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    }
    void tick()
    const t = setInterval(tick, 2000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [loopId])

  const phase = detail?.phase || ''
  const livePhase = stepperPhase(phase)
  const displayPhase = viewPhase ?? livePhase
  const isReview = Boolean(viewPhase && viewPhase !== livePhase)
  const liveIdx = phaseIndex(livePhase)

  const canVisit = useCallback(
    (p: string) => {
      const i = phaseIndex(p)
      if (i < 0 || liveIdx < 0) return false
      return i <= liveIdx
    },
    [liveIdx],
  )

  useEffect(() => {
    const oa = detail?.diagnosis_summary
    if (oa?.quality_diagnosis && !summary) {
      setSummary(oa.quality_diagnosis)
    }
    const opt = oa?.optimization_priority || []
    if (opt.length && selectedDirs.length === 0 && adoptAll) {
      setSelectedDirs(opt.map((_, i) => i + 1))
    }
  }, [detail?.diagnosis_summary, summary, selectedDirs.length, adoptAll])

  useEffect(() => {
    if (!loopId) return
    if (displayPhase !== 'OPTIMIZING' && displayPhase !== 'COMPARING' && displayPhase !== 'DONE') return
    void api.getPe(loopId, 'draft').then((r) => setDraft(r.data.content)).catch(() => {})
  }, [loopId, displayPhase])

  useEffect(() => {
    if (!loopId || !detail?.files?.annotation) {
      if (displayPhase !== 'WAITING_ANNOTATION') setAnnotation(null)
      return
    }
    if (displayPhase === 'WAITING_ANNOTATION' || displayPhase === 'OPTIMIZING') {
      void api
        .report(loopId, 'annotation')
        .then((r) => setAnnotation(r.data))
        .catch(() => setAnnotation(null))
    }
  }, [loopId, displayPhase, detail?.files?.annotation])

  useEffect(() => {
    if (!loopId) return
    if (displayPhase === 'RUNNING_V1' || displayPhase === 'DEEP_DIVING') {
      void api
        .evalPreview(loopId, 'v1')
        .then((r) => {
          if (r.data.exists) {
            setEvalPreview({
              version: 'v1',
              columns: r.data.columns,
              rows: r.data.rows,
              total: r.data.total,
            })
          } else setEvalPreview(null)
        })
        .catch(() => setEvalPreview(null))
    } else if (displayPhase === 'RUNNING_V2' || displayPhase === 'COMPARING') {
      void api
        .evalPreview(loopId, 'v2')
        .then((r) => {
          if (r.data.exists) {
            setEvalPreview({
              version: 'v2',
              columns: r.data.columns,
              rows: r.data.rows,
              total: r.data.total,
            })
          } else if (displayPhase === 'RUNNING_V2') setEvalPreview(null)
          else {
            void api.evalPreview(loopId, 'v1').then((r1) => {
              if (r1.data.exists) {
                setEvalPreview({
                  version: 'v1',
                  columns: r1.data.columns,
                  rows: r1.data.rows,
                  total: r1.data.total,
                })
              }
            })
          }
        })
        .catch(() => setEvalPreview(null))
    } else if (displayPhase === 'DONE') {
      void api
        .evalPreview(loopId, 'v2')
        .then((r) => {
          if (r.data.exists) {
            setEvalPreview({
              version: 'v2',
              columns: r.data.columns,
              rows: r.data.rows,
              total: r.data.total,
            })
          } else {
            return api.evalPreview(loopId, 'v1').then((r1) => {
              if (r1.data.exists) {
                setEvalPreview({
                  version: 'v1',
                  columns: r1.data.columns,
                  rows: r1.data.rows,
                  total: r1.data.total,
                })
              } else setEvalPreview(null)
            })
          }
        })
        .catch(() => setEvalPreview(null))
    } else {
      setEvalPreview(null)
    }
  }, [loopId, displayPhase])

  const jobState = detail?.job?.job_state || 'idle'

  const onStart = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await api.start({
        agent_id: agentId,
        dataset,
        scorer_id: scorerId,
        ignore_fail: true,
      })
      setLoopId(res.data.loop_id)
      setViewPhase(null)
      await loadMeta()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onAnnotate = async () => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.annotate(loopId, {
        business_summary: summary,
        adopted_indices: adoptAll ? [] : selectedDirs,
        adopt_all: adoptAll,
      })
      setViewPhase(null)
      const res = await api.getLoop(loopId)
      setDetail(res.data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onOptimizeAuto = async () => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.optimize(loopId, 'auto')
      const pe = await api.getPe(loopId, 'draft')
      setDraft(pe.data.content)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onSaveDraft = async () => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.putDraft(loopId, draft)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onAdopt = async () => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.putDraft(loopId, draft)
      await api.adoptDraft(loopId)
      setViewPhase(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onDecide = async (decision: 'ADOPTED' | 'ROLLED_BACK') => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.decide(loopId, decision, decideNote)
      setViewPhase(null)
      const res = await api.getLoop(loopId)
      setDetail(res.data)
      await loadMeta()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const onResumeFailed = async () => {
    if (!loopId || isReview) return
    setBusy(true)
    try {
      await api.resume(loopId, true)
      setViewPhase(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const optList = detail?.diagnosis_summary?.optimization_priority || []

  const wrapCols = useMemo(
    () => new Set(['生产回复', '得分说明', '执行结果']),
    [],
  )

  const activeGap = useMemo(() => {
    const g = detail?.batch_gaps
    if (!g) return null
    let ver = g.active || 'v1'
    if (displayPhase === 'RUNNING_V2' || displayPhase === 'COMPARING') ver = 'v2'
    else if (
      displayPhase === 'RUNNING_V1' ||
      displayPhase === 'DEEP_DIVING' ||
      displayPhase === 'WAITING_ANNOTATION'
    ) {
      ver = 'v1'
    } else if (displayPhase === 'DONE') {
      ver = (g.v1?.pending ?? 0) > 0 ? 'v1' : 'v2'
    }
    const gap = ver === 'v2' ? g.v2 : g.v1
    return { version: ver, ...(gap || {}) }
  }, [detail?.batch_gaps, displayPhase])

  return (
    <div className="app" data-theme="cobalt">
      <div className="shell">
        <aside className="rail">
          <div className="rail-brand">
            <span className="code">Facing Reply</span>
            <strong>评测闭环</strong>
          </div>
          <h2>历史 Loop</h2>
          <ul className="loop-list">
            {loops.length === 0 && <li className="muted" style={{ padding: '0.5rem' }}>暂无</li>}
            {[...loops].reverse().map((l) => (
              <li key={l.loop_id}>
                <button
                  type="button"
                  className={l.loop_id === loopId ? 'active' : ''}
                  onClick={() => setLoopId(l.loop_id)}
                >
                  <strong>{l.loop_id}</strong>
                  <span>{PHASE_LABEL[l.phase] || l.phase}</span>
                </button>
              </li>
            ))}
          </ul>
          {loopId && (
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setLoopId(null)
                setDetail(null)
              }}
            >
              新建闭环
            </button>
          )}
        </aside>

        <div className="workspace">
          <header className="topbar">
            <div>
              <h1>面谈客户回复 · 自进化闭环</h1>
              <p className="sub">选智能体、评测集与评分器，跑通 v1 → 诊断 → 校准 → PE → v2</p>
            </div>
            <div className="pills">
              {configOk === false && <span className="pill bad">缺少 config</span>}
            </div>
          </header>

          <main className="main">
            {error && <div className="banner error">{error}</div>}

            {!loopId && (
              <section className="panel">
                <h2>启动闭环</h2>
                <p className="lead">只选三项。后台会自动跑完 v1 与诊断，直到人工校准门控。</p>
                <div className="start-form">
                  <label>
                    智能体
                    <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                      {agents.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    评测集
                    <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
                      {datasets.map((d) => (
                        <option key={d.name} value={d.name}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    评分器
                    <select value={scorerId} onChange={(e) => setScorerId(e.target.value)}>
                      {scorers.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={busy || !configOk || !agentId || !dataset || !scorerId}
                    onClick={onStart}
                  >
                    {busy ? '启动中…' : '启动闭环'}
                  </button>
                </div>
              </section>
            )}

            {loopId && detail && (
            <>
              <nav className="stepper" aria-label="阶段">
                {PHASES.map((p) => {
                  const idx = phaseIndex(p)
                  const reachable = canVisit(p)
                  let cls = 'step'
                  if (p === displayPhase) cls += ' viewing'
                  if (p === livePhase) cls += ' current'
                  else if (reachable && idx < liveIdx) cls += ' done'
                  if (reachable) cls += ' clickable'
                  else cls += ' locked'
                  return (
                    <button
                      key={p}
                      type="button"
                      className={cls}
                      disabled={!reachable}
                      title={
                        reachable
                          ? p === livePhase
                            ? '当前阶段'
                            : '查看该阶段（不重跑）'
                          : '尚未到达'
                      }
                      onClick={() => {
                        if (!reachable) return
                        setViewPhase(p === livePhase ? null : p)
                      }}
                    >
                      <span className="dot" />
                      <span>{PHASE_LABEL[p]}</span>
                    </button>
                  )
                })}
              </nav>

              {!isReview && detail.job?.log_tail?.length > 0 && (
                <section className="panel status-panel">
                  <p className="muted">
                    作业：{jobState}
                    {detail.job?.message ? ` · ${detail.job.message}` : ''}
                  </p>
                  <pre className="log">{detail.job.log_tail.slice(-30).join('\n')}</pre>
                </section>
              )}

              {!isReview && phase === 'FAILED' && (
                <section className="panel">
                  <h2>跑批失败</h2>
                  <p>
                    常见原因：模型免费额度用尽（403 FreeTierOnly）。请到控制台充值或关闭「仅免费额度」后换 Key，再新开 loop。
                    重跑入口已暂时关闭。
                  </p>
                  <div className="actions">
                    <button type="button" className="btn primary" disabled={busy} onClick={() => void onResumeFailed()}>
                      忽略失败并继续
                    </button>
                  </div>
                </section>
              )}

              {(displayPhase === 'RUNNING_V1' ||
                displayPhase === 'DEEP_DIVING' ||
                displayPhase === 'RUNNING_V2' ||
                displayPhase === 'COMPARING' ||
                displayPhase === 'DONE') && (
                <section className="panel">
                  <h2>
                    {displayPhase === 'DEEP_DIVING'
                      ? '深钻诊断'
                      : displayPhase === 'RUNNING_V2'
                        ? 'V2 跑批'
                        : displayPhase === 'COMPARING' || displayPhase === 'DONE'
                          ? '测评明细'
                          : 'V1 跑批'}
                  </h2>
                  {!isReview && ['RUNNING_V1', 'DEEP_DIVING', 'RUNNING_V2'].includes(phase) && (
                    <p className="muted">正在调用模型跑批/诊断，页面每 2 秒刷新。</p>
                  )}
                  {!isReview && (activeGap?.pending ?? 0) > 0 && (
                    <p className="muted" style={{ marginBottom: '0.75rem' }}>
                      未成功 {activeGap?.pending} 条
                      {activeGap?.pending_reply ? `（回复缺 ${activeGap.pending_reply}` : ''}
                      {activeGap?.pending_score
                        ? `${activeGap?.pending_reply ? ' · ' : '（'}打分缺 ${activeGap.pending_score}`
                        : ''}
                      {activeGap?.pending_reply || activeGap?.pending_score ? '）' : ''}
                      。重跑已暂时关闭——请先确认模型额度/密钥。
                    </p>
                  )}
                  {displayPhase === 'DEEP_DIVING' && detail.diagnosis_summary && (
                    <div className="diag">
                      <p>
                        <strong>均分</strong> {detail.diagnosis_summary.avg_score} ·{' '}
                        <strong>样本</strong> {detail.diagnosis_summary.sample_count}
                      </p>
                      <p>
                        <strong>总评</strong> {detail.diagnosis_summary.quality_diagnosis}
                      </p>
                      <p>
                        <strong>瓶颈</strong> {detail.diagnosis_summary.key_bottleneck}
                      </p>
                    </div>
                  )}
                  {evalPreview && (
                    <>
                      <p className="muted">
                        {evalPreview.version.toUpperCase()} · 共 {evalPreview.total ?? evalPreview.rows.length}{' '}
                        行 · 含生产回复与全部维度打分
                        {(activeGap?.pending ?? 0) > 0
                          ? ` · 未成功 ${activeGap?.pending}`
                          : ''}
                      </p>
                      <div className="table-wrap table-wrap-full">
                        <table>
                          <thead>
                            <tr>
                              {evalPreview.columns.map((c) => (
                                <th key={c} className={wrapCols.has(c) ? 'col-wrap' : undefined}>
                                  {c}
                                </th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {evalPreview.rows.map((row, i) => (
                              <tr key={i}>
                                {evalPreview.columns.map((c) => (
                                  <td key={c} className={wrapCols.has(c) ? 'col-wrap' : undefined}>
                                    {String(row[c] ?? '')}
                                  </td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}
                  {!evalPreview && (
                    <p className="muted">尚无该版本测评文件可预览。</p>
                  )}
                </section>
              )}

              {displayPhase === 'WAITING_ANNOTATION' && (
                <section className="panel">
                  <h2>人工校准{isReview ? '（只读）' : ''}</h2>
                  {detail.diagnosis_summary ? (
                    <div className="diag">
                      <p>
                        <strong>均分</strong> {detail.diagnosis_summary.avg_score} ·{' '}
                        <strong>样本</strong> {detail.diagnosis_summary.sample_count}
                      </p>
                      <p>
                        <strong>总评</strong> {detail.diagnosis_summary.quality_diagnosis}
                      </p>
                      <p>
                        <strong>瓶颈</strong> {detail.diagnosis_summary.key_bottleneck}
                      </p>
                      <p>
                        <strong>系统问题</strong> {detail.diagnosis_summary.systemic_issue}
                      </p>
                    </div>
                  ) : (
                    <p className="muted">诊断摘要尚未就绪。</p>
                  )}

                  {isReview && annotation ? (
                    <>
                      <p>
                        <strong>已提交业务总评</strong> {String(annotation.business_summary || '')}
                      </p>
                      <p>
                        <strong>已采纳方向</strong>{' '}
                        {Array.isArray(annotation.adopted_directions)
                          ? (annotation.adopted_directions as unknown[]).length
                          : 0}{' '}
                        条
                      </p>
                      <ul className="dir-list muted">
                        {((annotation.adopted_directions as unknown[]) || []).map((item, i) => (
                          <li key={i}>{optLabel(item, i)}</li>
                        ))}
                      </ul>
                    </>
                  ) : (
                    <>
                      <label className="check">
                        <input
                          type="checkbox"
                          checked={adoptAll}
                          disabled={isReview}
                          onChange={(e) => setAdoptAll(e.target.checked)}
                        />
                        采纳全部优化方向
                      </label>
                      {!adoptAll && (
                        <ul className="dir-list">
                          {optList.map((item, i) => {
                            const n = i + 1
                            const checked = selectedDirs.includes(n)
                            return (
                              <li key={n}>
                                <label>
                                  <input
                                    type="checkbox"
                                    checked={checked}
                                    disabled={isReview}
                                    onChange={(e) => {
                                      setSelectedDirs((prev) =>
                                        e.target.checked
                                          ? [...prev, n].sort((a, b) => a - b)
                                          : prev.filter((x) => x !== n),
                                      )
                                    }}
                                  />
                                  {optLabel(item, i)}
                                </label>
                              </li>
                            )
                          })}
                        </ul>
                      )}
                      {adoptAll && (
                        <ul className="dir-list muted">
                          {optList.map((item, i) => (
                            <li key={i}>{optLabel(item, i)}</li>
                          ))}
                        </ul>
                      )}
                      <label>
                        业务总评语
                        <textarea
                          value={summary}
                          onChange={(e) => setSummary(e.target.value)}
                          rows={4}
                          disabled={isReview}
                        />
                      </label>
                      {!isReview && (
                        <button type="button" className="btn primary" disabled={busy} onClick={onAnnotate}>
                          确认提交校准
                        </button>
                      )}
                    </>
                  )}
                </section>
              )}

              {displayPhase === 'OPTIMIZING' && (
                <section className="panel">
                  <h2>PE 优化{isReview ? '（只读）' : ''}</h2>
                  <p className="lead">
                    {isReview
                      ? '查看当时的 PE 草稿，不会触发重新优化。'
                      : '可让 LLM 按校准结论改草稿，或直接编辑后采纳为 v2。'}
                  </p>
                  {!isReview && (
                    <div className="actions">
                      <button type="button" className="btn primary" disabled={busy} onClick={onOptimizeAuto}>
                        LLM 生成草稿
                      </button>
                      <button type="button" className="btn" disabled={busy} onClick={onSaveDraft}>
                        保存草稿
                      </button>
                      <button type="button" className="btn accent" disabled={busy} onClick={onAdopt}>
                        采纳为 v2 并跑批
                      </button>
                    </div>
                  )}
                  <label>
                    pe/draft.md
                    <textarea
                      className="pe-editor"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      rows={22}
                      spellCheck={false}
                      readOnly={isReview}
                    />
                  </label>
                </section>
              )}

              {displayPhase === 'COMPARING' && (
                <section className="panel">
                  <h2>v1 / v2 对比{isReview ? '（只读）' : ''}</h2>
                  {detail.compare ? (
                    <>
                      <div className="compare-grid">
                        <div className="stat">
                          <span className="stat-label">V1 均分</span>
                          <span className="stat-value">{detail.compare.v1?.avg_score ?? '—'}</span>
                        </div>
                        <div className="stat">
                          <span className="stat-label">V2 均分</span>
                          <span className="stat-value">{detail.compare.v2?.avg_score ?? '—'}</span>
                        </div>
                        <div className="stat">
                          <span className="stat-label">Δ</span>
                          <span className="stat-value">{detail.compare.delta_avg ?? '—'}</span>
                        </div>
                      </div>
                      <p>
                        <strong>建议</strong> {detail.compare.suggestion} — {detail.compare.suggestion_reason}
                      </p>
                      <p>
                        <strong>改善维度</strong>{' '}
                        {(detail.compare.improved_dimensions || []).join('、') || '无'}
                      </p>
                      <p>
                        <strong>退步维度</strong>{' '}
                        {(detail.compare.regressed_dimensions || []).join('、') || '无'}
                      </p>
                    </>
                  ) : (
                    <p className="muted">对比报告尚未生成。</p>
                  )}
                  {!isReview && detail.compare && (
                    <>
                      <label>
                        备注
                        <input value={decideNote} onChange={(e) => setDecideNote(e.target.value)} />
                      </label>
                      <div className="actions">
                        <button
                          type="button"
                          className="btn accent"
                          disabled={busy}
                          onClick={() => onDecide('ADOPTED')}
                        >
                          采纳 v2
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={busy}
                          onClick={() => onDecide('ROLLED_BACK')}
                        >
                          回滚到 v1
                        </button>
                      </div>
                    </>
                  )}
                </section>
              )}

              {displayPhase === 'DONE' && (
                <section className="panel">
                  <h2>闭环完成</h2>
                  <pre className="log">{JSON.stringify(detail.decision, null, 2)}</pre>
                  {!isReview && (
                    <button
                      type="button"
                      className="btn primary"
                      onClick={() => {
                        setLoopId(null)
                        setDetail(null)
                      }}
                    >
                      再开一轮
                    </button>
                  )}
                </section>
              )}
            </>
            )}
          </main>
        </div>
      </div>
    </div>
  )
}
