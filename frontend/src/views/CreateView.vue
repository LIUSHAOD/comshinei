<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as api from '../api'

const router = useRouter()

const photo = ref<File | null>(null)
const previewUrl = ref('')
const requirements = ref('')
const submitting = ref(false)
const errorMsg = ref('')
const dragging = ref(false)
const fileInput = ref<HTMLInputElement>()

const ACCEPT = ['image/jpeg', 'image/png', 'image/webp']

const canSubmit = computed(
  () => !!photo.value && !!requirements.value.trim() && !submitting.value,
)

function pickFile(f: File | null | undefined) {
  if (!f) return
  if (!ACCEPT.includes(f.type)) {
    errorMsg.value = '仅支持 jpg / png / webp 格式图片'
    return
  }
  errorMsg.value = ''
  photo.value = f
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = URL.createObjectURL(f)
}

function onDrop(e: DragEvent) {
  dragging.value = false
  pickFile(e.dataTransfer?.files?.[0])
}

function onInputChange(e: Event) {
  const el = e.target as HTMLInputElement
  pickFile(el.files?.[0])
  el.value = ''
}

function clearPhoto() {
  photo.value = null
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
}

async function submit() {
  if (!canSubmit.value || !photo.value) return
  submitting.value = true
  errorMsg.value = ''
  try {
    const r = await api.createProject(photo.value, requirements.value.trim())
    router.push(`/design/${r.id}`)
  } catch (e) {
    errorMsg.value = (e as Error).message
  } finally {
    submitting.value = false
  }
}

onBeforeUnmount(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})
</script>

<template>
  <div class="grid grid-2">
    <div class="card">
      <h2 class="card-title">上传实拍图</h2>
      <div
        v-if="!previewUrl"
        class="upload-zone"
        :class="{ dragging }"
        @click="fileInput?.click()"
        @dragover.prevent="dragging = true"
        @dragleave="dragging = false"
        @drop.prevent="onDrop"
      >
        <div class="icon">＋</div>
        <div>拖拽图片到这里，或点击选择</div>
        <div class="hint mt-12">支持 jpg / png / webp</div>
      </div>
      <div v-else>
        <div class="img-frame">
          <img :src="previewUrl" alt="实拍图预览" />
        </div>
        <div class="row between mt-12">
          <span class="hint">{{ photo?.name }}</span>
          <button class="btn btn-sm" @click="clearPhoto">重新选择</button>
        </div>
      </div>
      <input
        ref="fileInput"
        type="file"
        accept="image/jpeg,image/png,image/webp"
        hidden
        @change="onInputChange"
      />
    </div>

    <div class="card">
      <h2 class="card-title">设计需求</h2>
      <textarea
        v-model="requirements"
        class="textarea"
        rows="6"
        placeholder="描述你想要的风格与要求，例如：北欧风客厅，暖色调，保留原有沙发，增加绿植……"
      ></textarea>
      <p v-if="errorMsg" class="error-text mt-12">{{ errorMsg }}</p>
      <div class="row between mt-16">
        <router-link to="/gallery" class="hint">没有灵感？去风格库看看 →</router-link>
        <button class="btn btn-primary" :disabled="!canSubmit" @click="submit">
          <span v-if="submitting" class="spin"></span>
          {{ submitting ? '提交中…' : '开始设计' }}
        </button>
      </div>
    </div>
  </div>
</template>
