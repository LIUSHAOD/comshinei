import { onBeforeUnmount } from 'vue'

export type SseHandler<T = unknown> = (data: T, raw: MessageEvent) => void

/**
 * EventSource 封装：
 * - on(type, cb) 注册具名事件（JSON data 自动解析，心跳注释由浏览器原生忽略）
 * - 断线由原生 EventSource 自动重连并带 Last-Event-ID，后端按 seq 续传
 * - 终态（completed / error 且 fatal）自动 close，避免浏览器把正常收尾当断线重连
 * - 组件卸载自动 close
 */
export function useSSE() {
  let source: EventSource | null = null
  const handlers = new Map<string, SseHandler[]>()

  function dispatch(ev: MessageEvent) {
    let data: unknown = null
    try {
      data = JSON.parse(ev.data)
    } catch {
      return
    }
    for (const cb of handlers.get(ev.type) ?? []) {
      ;(cb as SseHandler)(data, ev)
    }
    const fatal = ev.type === 'error' && Boolean((data as { fatal?: boolean })?.fatal)
    if (ev.type === 'completed' || fatal) close()
  }

  function on<T>(type: string, cb: SseHandler<T>): void {
    const list = handlers.get(type) ?? []
    list.push(cb as SseHandler)
    handlers.set(type, list)
    // open 之后再注册也要能收到事件
    source?.addEventListener(type, dispatch)
  }

  function open(url: string): void {
    close()
    source = new EventSource(url)
    for (const type of handlers.keys()) {
      source.addEventListener(type, dispatch)
    }
  }

  function close(): void {
    source?.close()
    source = null
  }

  onBeforeUnmount(close)

  return { on, open, close }
}
