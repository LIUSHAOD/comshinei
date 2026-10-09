<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import * as api from '../api'
import type { StyleImage, StyleUploadFailedItem } from '../types'

const PAGE_SIZE = 50
const MAX_BATCH = 50

const items = ref<StyleImage[]>([])
const total = ref(0)
const loading = ref(false)
const loadingMore = ref(false)
const errorMsg = ref('')

const checked = ref<string[]>([])
const deleting = ref(false)

const uploading = ref(false)
const uploadPercent = ref(0)
const uploadFailed = ref<StyleUploadFailedItem[]>([])
const uploadNote = ref('')
const dragging = ref(false)
const fileInput = ref<HTMLInputElement>()

const allChecked = computed(
  () => items.value.length > 0 && checked.value.length === items.value.length,
)
const hasMore = computed(() => items.value.length < total.value)

function fmtTime(s: string | null): string {
  if (!s) return '-'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString('zh-CN', { hour12: false })
}

async function load(reset = true) {
  if (reset) {
    loading.value = true
  } else {
    loadingMore.value = true
  }
  errorMsg.value = ''
  try {
    const skip = reset ? 0 : items.value.length
    const r = await api.listStyles(skip, PAGE_SIZE)
    total.value = r.total
    items.value = reset ? r.records : [...items.value, ...r.records]
    if (reset) checked.value = []
  } catch (e) {
    errorMsg.value = (e as Error).message
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function toggle(id: string) {
  checked.value = checked.value.includes(id)
    ? checked.value.filter((x) => x !== id)
    : [...checked.value, id]
}

function toggleAll() {
  checked.value = allChecked.value ? [] : items.value.map((x) => x.id)
}

async function uploadFiles(files: FileList | File[] | null | undefined) {
  const list = Array.from(files ?? [])
  if (!list.length || uploading.value) return
  uploadFailed.value = []
  uploadNote.value = ''
  if (list.length > MAX_BATCH) {
    uploadFailed.value = [
      { filename: `${list.length} 个文件`, reason: `单批次最多 ${MAX_BATCH} 张，请分批上传` },
    ]
    return
  }
  uploading.value = true
  uploadPercent.value = 0
  try {
    const r = await api.uploadStyles(list, (p) => (uploadPercent.value = p))
    uploadFailed.value = r.failed
    uploadNote.value = `成功导入 ${r.imported} 张${r.failed.length ? `，失败 ${r.failed.length} 张` : ''}`
    await load(true)
  } catch (e) {
    errorMsg.value = (e as Error).message
  } finally {
    uploading.value = false
  }
}

function onDrop(e: DragEvent) {
  dragging.value = false
  uploadFiles(e.dataTransfer?.files)
}

function onInputChange(e: Event) {
  const el = e.target as HTMLInputElement
  uploadFiles(el.files)
  el.value = ''
}

async function removeOne(id: string) {
  if (!window.confirm('删除这张风格图？（同步从检索库移除）')) return
  try {
    await api.deleteStyle(id)
    items.value = items.value.filter((x) => x.id !== id)
    checked.value = checked.value.filter((x) => x !== id)
    total.value = Math.max(0, total.value - 1)
  } catch (e) {
    errorMsg.value = (e as Error).message
  }
}

async function removeChecked() {
  if (!checked.value.length || deleting.value) return
  if (!window.confirm(`删除选中的 ${checked.value.length} 张风格图？`)) return
  deleting.value = true
  errorMsg.value = ''
  const ids = [...checked.value]
  const results = await Promise.allSettled(ids.map((id) => api.deleteStyle(id)))
  const failed = results.filter((r) => r.status === 'rejected').length
  if (failed) errorMsg.value = `${failed} 张删除失败，可重试`
  checked.value = []
  deleting.value = false
  await load(true)
}

onMounted(() => load(true))
</script>

<template>
  <div class="grid" style="grid-template-columns: 1fr">
    <div class="card">
      <h2 class="card-title">
        上传风格图
        <span class="sub">批量上传后自动向量化并进入检索库（单批最多 {{ MAX_BATCH }} 张）</span>
      </h2>
      <div
        class="upload-zone"
        :class="{ dragging }"
        @click="fileInput?.click()"
        @dragover.prevent="dragging = true"
        @dragleave="dragging = false"
        @drop.prevent="onDrop"
      >
        <template v-if="uploading">
          <div>上传中 {{ uploadPercent }}%</div>
          <div class="progress-track mt-12" style="max-width: 320px; margin: 0 auto">
            <div class="progress-fill" :style="{ width: uploadPercent + '%' }"></div>
          </div>
        </template>
        <template v-else>
          <div class="icon">⇪</div>
          <div>拖拽多张图片到这里，或点击选择（可多选）</div>
        </template>
      </div>
      <input
        ref="fileInput"
        type="file"
        accept="image/*"
        multiple
        hidden
        @change="onInputChange"
      />
      <p v-if="uploadNote" class="hint mt-12">{{ uploadNote }}</p>
      <div v-if="uploadFailed.length" class="mt-12">
        <p class="error-text">以下文件未导入：</p>
        <p v-for="f in uploadFailed" :key="f.filename" class="error-text">
          {{ f.filename }} — {{ f.reason }}
        </p>
      </div>
    </div>

    <div class="card">
      <div class="row between wrap">
        <h2 class="card-title" style="margin-bottom: 0">
          风格库
          <span class="sub">共 {{ total }} 张</span>
        </h2>
        <div class="row">
          <label class="hint row" style="gap: 6px; cursor: pointer">
            <input
              type="checkbox"
              :checked="allChecked"
              style="accent-color: var(--primary)"
              @change="toggleAll"
            />
            全选
          </label>
          <button
            class="btn btn-danger btn-sm"
            :disabled="!checked.length || deleting"
            @click="removeChecked"
          >
            <span v-if="deleting" class="spin"></span>
            删除选中（{{ checked.length }}）
          </button>
          <button class="btn btn-sm" :disabled="loading" @click="load(true)">刷新</button>
        </div>
      </div>

      <p v-if="errorMsg" class="error-text mt-12">{{ errorMsg }}</p>
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!items.length" class="empty">风格库为空，先上传一些风格图吧</div>
      <template v-else>
        <div class="thumb-grid mt-16">
          <div v-for="s in items" :key="s.id" class="thumb">
            <img :src="s.image_url" :alt="s.id" loading="lazy" />
            <input
              class="check"
              type="checkbox"
              :checked="checked.includes(s.id)"
              @change="toggle(s.id)"
            />
            <button class="del" title="删除" @click="removeOne(s.id)">×</button>
            <div class="overlay">
              <div>来源：{{ s.source }}</div>
              <div>{{ fmtTime(s.created_at) }}</div>
            </div>
          </div>
        </div>
        <div v-if="hasMore" class="mt-16" style="text-align: center">
          <button class="btn" :disabled="loadingMore" @click="load(false)">
            <span v-if="loadingMore" class="spin"></span>
            加载更多（{{ items.length }} / {{ total }}）
          </button>
        </div>
      </template>
    </div>
  </div>
</template>
