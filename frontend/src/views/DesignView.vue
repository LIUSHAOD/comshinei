<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import * as api from '../api'
import { useProjectStore } from '../stores/project'
import { useSSE } from '../composables/useSSE'
import type {
  CandidatesData,
  CompletedData,
  ErrorData,
  ImageDoneData,
  LineartDoneData,
  ProgressData,
  Stage,
  StageChangeData,
} from '../types'

const STAGE_LABELS: Record<string, string> = {
  created: '已创建',
  lineart: '线稿提取中',
  selecting: '待选择风格',
  prompting: '提示词生成中',
  generating: '效果图生成中',
  done: '已完成',
  failed: '失败',
}

const NODE_LABELS: Record<string, string> = {
  lineart: '线稿提取',
  retrieve: '风格检索',
  prompt: '提示词生成',
  generate: '图像生成',
}

const route = useRoute()
const store = useProjectStore()
const sse = useSSE()

const id = route.params.id as string

const loadError = ref('')
const actionError = ref('')
const requerying = ref(false)
const confirming = ref(false)
const cancelling = ref(false)
const logEl = ref<HTMLElement>()

const stageLabel = computed(() => STAGE_LABELS[store.stage ?? ''] ?? store.stage)
const progressNodeLabel = computed(
  () => NODE_LABELS[store.progress?.node ?? ''] ?? store.progress?.node ?? '',
)
const lineartPercent = computed(() =>
  store.progress?.node === 'lineart' ? store.progress.percent : null,
)

function openStream() {
  sse.open(api.streamUrl(id))
}

async function doRequery() {
  if (!store.canRequery || requerying.value) return
  requerying.value = true
  actionError.value = ''
  try {
    await api.requeryProject(id)
    store.startRound('requery')
    openStream() // 新一轮自包含事件流（旧事件已被后端清空，以 completed 终止）
  } catch (e) {
    actionError.value = (e as Error).message
  } finally {
    requerying.value = false
  }
}

async function doConfirm() {
  if (!store.canConfirm || confirming.value || !store.selectedId) return
  confirming.value = true
  actionError.value = ''
  try {
    await api.confirmProject(id, store.selectedId)
    store.startRound('confirm')
    openStream()
  } catch (e) {
    actionError.value = (e as Error).message
  } finally {
    confirming.value = false
  }
}

async function doCancel() {
  if (!store.canCancel || cancelling.value) return
  cancelling.value = true
  actionError.value = ''
  try {
    const r = await api.cancelProject(id)
    store.applyCancel(r.stage) // 后端同时推 completed 收尾事件流
  } catch (e) {
    actionError.value = (e as Error).message
  } finally {
    cancelling.value = false
  }
}

// 事件日志自动滚到底
watch(
  () => store.events.length,
  async () => {
    await nextTick()
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
  },
)

onMounted(async () => {
  store.reset()

  sse.on<StageChangeData>('stage_change', (d) => store.onStageChange(d))
  sse.on<ProgressData>('progress', (d) => {
    store.onProgress(d)
    if (d.prompt && store.project) store.project.prompt = d.prompt
  })
  sse.on<LineartDoneData>('lineart_done', (d) => store.onLineartDone(d))
  sse.on<CandidatesData>('candidates', (d) => store.onCandidates(d))
  sse.on<ImageDoneData>('image_done', (d) => store.onImageDone(d))
  sse.on<ErrorData>('error', (d) => store.onError(d))
  sse.on<CompletedData>('completed', (d) => store.onCompleted(d))

  try {
    await store.fetchProject(id)
    if (store.project?.created_at) {
      store.log('info', `已载入项目（${stageLabel.value}）`)
    }
  } catch (e) {
    loadError.value = (e as Error).message
    return
  }
  // 终态（done/failed）不开 SSE；其余阶段打开断线自动续传
  if (!store.isTerminal) openStream()
})
</script>

<template>
  <div v-if="loadError" class="card">
    <p class="error-text">加载项目失败：{{ loadError }}</p>
    <router-link to="/" class="hint">← 返回创建</router-link>
  </div>

  <template v-else-if="store.project">
    <div class="card row between wrap">
      <div class="row wrap">
        <strong>设计项目</strong>
        <span class="hint">#{{ id.slice(0, 8) }}</span>
        <span class="badge" :class="`stage-${store.stage}`">{{ stageLabel }}</span>
      </div>
      <div class="row">
        <span v-if="store.project.requirements" class="hint">
          需求：{{ store.project.requirements }}
        </span>
        <button class="btn btn-danger btn-sm" :disabled="!store.canCancel || cancelling" @click="doCancel">
          <span v-if="cancelling" class="spin"></span>
          取消任务
        </button>
      </div>
    </div>

    <p v-if="actionError" class="error-text mt-12">{{ actionError }}</p>
    <div v-if="store.stage === 'failed'" class="card mt-12" style="border-color: #f3c2c4">
      <p class="error-text">任务失败：{{ store.project.error || '未知错误' }}</p>
      <p v-if="store.project.lineart_url" class="hint mt-12">
        阶段 1 产物已保留——重新选择一张风格图后点「重试生成」即可，无需重新上传。
      </p>
    </div>

    <div class="grid design-top mt-16">
      <!-- 左：实拍图 + 线稿 -->
      <div class="grid" style="grid-template-columns: 1fr">
        <div class="card">
          <h3 class="card-title">实拍图</h3>
          <div class="img-frame">
            <img v-if="store.project.photo_url" :src="store.project.photo_url" alt="实拍图" />
            <div v-else class="img-placeholder">无实拍图</div>
          </div>
        </div>
        <div class="card">
          <h3 class="card-title">
            线稿
            <span v-if="lineartPercent !== null" class="sub">{{ lineartPercent }}%</span>
          </h3>
          <div v-if="store.project.lineart_url" class="img-frame">
            <img :src="store.project.lineart_url" alt="线稿" />
          </div>
          <div v-else class="skeleton img-placeholder">
            <template v-if="store.stage === 'lineart'">
              <span>线稿提取中…</span>
              <span v-if="lineartPercent !== null" class="hint">{{ lineartPercent }}%</span>
            </template>
            <span v-else>尚未生成</span>
          </div>
          <div v-if="store.project.mlsd_url || store.project.depth_url" class="row mt-12">
            <div v-if="store.project.mlsd_url" class="img-frame" style="flex: 1">
              <img :src="store.project.mlsd_url" alt="MLSD" title="MLSD" />
            </div>
            <div v-if="store.project.depth_url" class="img-frame" style="flex: 1">
              <img :src="store.project.depth_url" alt="深度图" title="深度图" />
            </div>
          </div>
        </div>
      </div>

      <!-- 右：候选风格 -->
      <div class="card">
        <div class="row between wrap">
          <h3 class="card-title" style="margin-bottom: 0">
            候选风格
            <span class="sub">点击选择一张作为生成参考</span>
          </h3>
          <div class="row">
            <button
              class="btn btn-sm"
              :disabled="!store.canRequery || requerying"
              @click="doRequery"
            >
              <span v-if="requerying || store.awaitingCandidates" class="spin"></span>
              {{ store.awaitingCandidates ? '检索中…' : '重新查询' }}
            </button>
            <button
              class="btn btn-primary btn-sm"
              :disabled="!store.canConfirm || confirming"
              @click="doConfirm"
            >
              <span v-if="confirming" class="spin"></span>
              {{ store.stage === 'failed' ? '重试生成' : '确认生成' }}
            </button>
          </div>
        </div>

        <div v-if="store.awaitingCandidates" class="empty">正在检索新一批候选…</div>
        <div v-else-if="!store.candidates.length" class="empty">
          {{ store.isRunning ? '等待候选结果…' : '暂无候选风格图' }}
        </div>
        <div v-else class="thumb-grid mt-16">
          <div
            v-for="c in store.candidates"
            :key="c.id"
            class="thumb clickable"
            :class="{ selected: store.selectedId === c.id }"
            @click="store.select(c.id)"
          >
            <img :src="c.image_url" :alt="c.id" loading="lazy" />
            <span class="score">{{ c.score.toFixed(3) }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 底部：进度 + 事件 + 结果 -->
    <div class="grid grid-2 mt-16">
      <div class="card">
        <h3 class="card-title">
          进度
          <span v-if="store.progress" class="sub">
            {{ progressNodeLabel }} {{ store.progress.percent }}%
          </span>
        </h3>
        <div class="progress-track">
          <div
            class="progress-fill"
            :style="{ width: (store.progress?.percent ?? 0) + '%' }"
          ></div>
        </div>
        <p v-if="store.progress?.message" class="hint mt-12">{{ store.progress.message }}</p>
        <div ref="logEl" class="event-log mt-12">
          <div v-if="!store.events.length" class="row">等待事件…</div>
          <div
            v-for="ev in store.events"
            :key="ev.seq"
            class="row"
            :class="{ err: ev.type === 'error' }"
          >
            <span class="t">{{ ev.time }}</span>
            <span class="text">{{ ev.text }}</span>
          </div>
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">最终效果图</h3>
        <template v-if="store.project.result_url">
          <div class="img-frame">
            <img :src="store.project.result_url" alt="最终效果图" />
          </div>
          <div class="row between mt-12">
            <a
              class="btn btn-primary btn-sm"
              :href="store.project.result_url"
              :download="`comshinei-${id.slice(0, 8)}.png`"
            >
              下载效果图
            </a>
          </div>
        </template>
        <div v-else-if="store.stage === 'generating'" class="skeleton img-placeholder">
          <span>效果图生成中…</span>
          <span v-if="store.progress?.node === 'generate'" class="hint">
            {{ store.progress.percent }}%
          </span>
        </div>
        <div v-else class="img-placeholder"><span>确认风格后生成</span></div>
        <div v-if="store.project.prompt" class="mt-12">
          <p class="hint" style="word-break: break-all">
            <strong>Prompt：</strong>{{ store.project.prompt }}
          </p>
        </div>
      </div>
    </div>
  </template>

  <div v-else class="card empty">加载中…</div>
</template>
