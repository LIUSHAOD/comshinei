/** 与后端 API 契约对应的类型定义（统一信封已被 axios 拦截器解包） */

export type Stage =
  | 'created'
  | 'lineart'
  | 'selecting'
  | 'prompting'
  | 'generating'
  | 'done'
  | 'failed'

export interface Candidate {
  id: string
  image_url: string
  score: number
}

export interface Project {
  id: string
  stage: Stage
  requirements: string
  photo_url: string | null
  lineart_url: string | null
  mlsd_url: string | null
  depth_url: string | null
  candidates: Candidate[]
  exclude_ids: string[]
  prompt: string | null
  negative_prompt: string | null
  result_url: string | null
  error: string | null
  created_at: string | null
}

export interface CreateProjectResult {
  id: string
  stage: Stage
  stream_url: string
}

export interface ConfirmResult {
  id: string
  stage: Stage
  stream_url: string
}

export interface CancelResult {
  stage: Stage
  comfy_interrupted: boolean
}

export interface RequeryResult {
  excluded_added: number
  exclude_total: number
}

export interface StyleImage {
  id: string
  image_url: string
  source: string
  created_at: string | null
}

export interface StyleListResult {
  total: number
  records: StyleImage[]
}

export interface StyleUploadFailedItem {
  filename: string
  reason: string
}

export interface StyleUploadResult {
  imported: number
  failed: StyleUploadFailedItem[]
  items: StyleImage[]
}

/* ---------- SSE 事件 data 载荷 ---------- */

export interface StageChangeData {
  stage: Stage
  message: string
}

export interface ProgressData {
  node: 'lineart' | 'retrieve' | 'prompt' | 'generate' | string
  percent: number
  message: string
  prompt?: string
}

export interface LineartDoneData {
  lineart_url: string
  mlsd_url: string
  depth_url: string
  seg_url?: string
}

export interface CandidatesData {
  candidates: Candidate[]
  excluded: string[]
}

export interface ImageDoneData {
  result_url: string
  elapsed_sec: number
}

export interface ErrorData {
  node: string
  message: string
  fatal?: boolean
}

/** completed 事件是当轮快照，字段按轮次不同（白名单合并） */
export interface CompletedData {
  stage: Stage
  lineart_url?: string | null
  mlsd_url?: string | null
  depth_url?: string | null
  candidates?: Candidate[]
  result_url?: string | null
  prompt?: string | null
  negative_prompt?: string | null
  cancelled?: boolean
  message?: string
}

export type SseEventType =
  | 'stage_change'
  | 'progress'
  | 'lineart_done'
  | 'candidates'
  | 'image_done'
  | 'error'
  | 'completed'
