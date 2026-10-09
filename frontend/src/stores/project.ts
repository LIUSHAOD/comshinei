import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api'
import type {
  Candidate,
  CandidatesData,
  CompletedData,
  ErrorData,
  ImageDoneData,
  LineartDoneData,
  ProgressData,
  Project,
  Stage,
  StageChangeData,
} from '../types'

export interface ProgressState {
  node: string
  percent: number
  message: string
}

export interface EventLogItem {
  seq: number
  type: string
  text: string
  time: string
}

const RUNNING_STAGES: Stage[] = ['created', 'lineart', 'prompting', 'generating']
const TERMINAL_STAGES: Stage[] = ['done', 'failed']
const CANCELLABLE_STAGES: Stage[] = ['lineart', 'prompting', 'generating']

/** completed 快照里允许合并进 project 的字段（白名单，忽略 message/cancelled 等） */
const SNAPSHOT_KEYS = [
  'stage',
  'lineart_url',
  'mlsd_url',
  'depth_url',
  'candidates',
  'result_url',
  'prompt',
  'negative_prompt',
] as const

let logSeq = 0

export const useProjectStore = defineStore('project', () => {
  const project = ref<Project | null>(null)
  const selectedId = ref<string | null>(null)
  const progress = ref<ProgressState | null>(null)
  const events = ref<EventLogItem[]>([])
  /** requery 后等待新一轮 candidates（驱动按钮 loading） */
  const awaitingCandidates = ref(false)

  const stage = computed<Stage | null>(() => project.value?.stage ?? null)
  const isRunning = computed(() => !!stage.value && RUNNING_STAGES.includes(stage.value))
  const isTerminal = computed(() => !!stage.value && TERMINAL_STAGES.includes(stage.value))
  const canCancel = computed(() => !!stage.value && CANCELLABLE_STAGES.includes(stage.value))
  const canRequery = computed(() => stage.value === 'selecting' && !awaitingCandidates.value)
  // failed 且阶段 1 产物在（有线稿）时允许重试 confirm（后端产物检查兜底）
  const canConfirm = computed(
    () =>
      (stage.value === 'selecting' ||
        (stage.value === 'failed' && !!project.value?.lineart_url)) &&
      !!selectedId.value,
  )
  const candidates = computed<Candidate[]>(() => project.value?.candidates ?? [])

  function log(type: string, text: string) {
    events.value.push({
      seq: ++logSeq,
      type,
      text,
      time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    })
    if (events.value.length > 200) events.value.splice(0, events.value.length - 200)
  }

  async function fetchProject(id: string) {
    const p = await api.getProject(id)
    project.value = p
    if (selectedId.value && !p.candidates.some((c) => c.id === selectedId.value)) {
      selectedId.value = null
    }
  }

  /* ---------- SSE 事件应用 ---------- */

  function onStageChange(d: StageChangeData) {
    if (project.value) project.value.stage = d.stage
    log('stage', d.message || `阶段切换：${d.stage}`)
  }

  function onProgress(d: ProgressData) {
    progress.value = { node: d.node, percent: d.percent ?? 0, message: d.message ?? '' }
  }

  function onLineartDone(d: LineartDoneData) {
    if (project.value) {
      project.value.lineart_url = d.lineart_url
      project.value.mlsd_url = d.mlsd_url
      project.value.depth_url = d.depth_url
    }
    log('lineart', '线稿提取完成')
  }

  function onCandidates(d: CandidatesData) {
    if (project.value) project.value.candidates = d.candidates ?? []
    selectedId.value = null
    awaitingCandidates.value = false
    log('candidates', `检索到 ${d.candidates?.length ?? 0} 张候选风格图`)
  }

  function onImageDone(d: ImageDoneData) {
    if (project.value) project.value.result_url = d.result_url
    log('image', `效果图生成完成，耗时 ${d.elapsed_sec?.toFixed?.(1) ?? '-'}s`)
  }

  function onError(d: ErrorData) {
    log('error', `[${d.node}] ${d.message}`)
    if (d.fatal) {
      awaitingCandidates.value = false
      progress.value = null
      if (project.value) {
        project.value.stage = 'failed'
        project.value.error = d.message
      }
    }
  }

  function onCompleted(d: CompletedData) {
    if (project.value) {
      for (const k of SNAPSHOT_KEYS) {
        if (d[k] !== undefined) (project.value as Record<string, unknown>)[k] = d[k]
      }
    }
    progress.value = null
    awaitingCandidates.value = false
    if (d.message) log('completed', d.message)
  }

  /* ---------- 动作触发的轮次切换 ---------- */

  /** requery / confirm 后开启新一轮自包含事件流前的现场清理 */
  function startRound(kind: 'requery' | 'confirm') {
    events.value = []
    progress.value = null
    if (kind === 'requery') {
      awaitingCandidates.value = true
      if (project.value) project.value.candidates = []
      selectedId.value = null
    } else {
      if (project.value) project.value.result_url = null
    }
  }

  function applyCancel(stage: Stage) {
    if (project.value) {
      project.value.stage = stage
      if (stage === 'failed' && !project.value.error) project.value.error = '用户取消'
    }
    progress.value = null
  }

  function select(id: string) {
    selectedId.value = selectedId.value === id ? null : id
  }

  function reset() {
    project.value = null
    selectedId.value = null
    progress.value = null
    events.value = []
    awaitingCandidates.value = false
  }

  return {
    project,
    selectedId,
    progress,
    events,
    awaitingCandidates,
    stage,
    isRunning,
    isTerminal,
    canCancel,
    canRequery,
    canConfirm,
    candidates,
    log,
    fetchProject,
    onStageChange,
    onProgress,
    onLineartDone,
    onCandidates,
    onImageDone,
    onError,
    onCompleted,
    startRound,
    applyCancel,
    select,
    reset,
  }
})
