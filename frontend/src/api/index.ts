import axios from 'axios'
import type { AxiosError, AxiosResponse } from 'axios'
import type {
  CancelResult,
  ConfirmResult,
  CreateProjectResult,
  Project,
  RequeryResult,
  StyleListResult,
  StyleUploadResult,
} from '../types'

/** 统一信封 {code, message, data}：成功解包 data；失败抛 message */
const http = axios.create({ baseURL: '/api', timeout: 60_000 })

http.interceptors.response.use(
  (response) => response.data?.data,
  (err: AxiosError<{ message?: string }>) =>
    Promise.reject(new Error(err.response?.data?.message || err.message || '网络错误')),
)

// 拦截器在运行时已把返回值替换为 data 本体，这里只做类型层对齐
function unwrap<T>(p: Promise<AxiosResponse<T>>): Promise<T> {
  return p as unknown as Promise<T>
}

/* ---------- 项目 ---------- */

export function createProject(photo: File, requirements: string): Promise<CreateProjectResult> {
  const fd = new FormData()
  fd.append('photo', photo)
  fd.append('requirements', requirements)
  return unwrap(http.post('/projects', fd, { timeout: 300_000 }))
}

export function getProject(id: string): Promise<Project> {
  return unwrap(http.get(`/projects/${id}`))
}

export function requeryProject(id: string): Promise<RequeryResult> {
  return unwrap(http.post(`/projects/${id}/requery`))
}

export function confirmProject(id: string, styleImageId: string): Promise<ConfirmResult> {
  return unwrap(http.post(`/projects/${id}/confirm`, { style_image_id: styleImageId }))
}

export function cancelProject(id: string): Promise<CancelResult> {
  return unwrap(http.post(`/projects/${id}/cancel`))
}

export function streamUrl(id: string): string {
  return `/api/projects/${id}/stream`
}

/* ---------- 风格库 ---------- */

export function listStyles(skip = 0, limit = 50): Promise<StyleListResult> {
  return unwrap(http.get('/styles', { params: { skip, limit } }))
}

export function uploadStyles(
  files: File[],
  onProgress?: (percent: number) => void,
): Promise<StyleUploadResult> {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)
  return unwrap(
    http.post('/styles', fd, {
      timeout: 600_000,
      onUploadProgress: (e) => {
        if (e.total) onProgress?.(Math.round((e.loaded / e.total) * 100))
      },
    }),
  )
}

export function deleteStyle(id: string): Promise<void> {
  return unwrap(http.delete(`/styles/${id}`))
}
