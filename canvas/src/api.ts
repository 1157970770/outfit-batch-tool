import type { ImageGenOptions } from './types'

export type Bootstrap = {
  ok: true
  user: { username: string; role?: string }
  service: { imagegen: ImageGenOptions }
}

export type TaskImage = {
  index: number
  name: string
  width?: number | null
  height?: number | null
  url: string
  download_url: string
}

export type ImageGenTask = {
  id: string
  status: string
  progress: number
  error: string
  images: TaskImage[]
  active: boolean
  elapsed_ms?: number
}

export type UploadResult = {
  upload_id: string
  name: string
  width?: number | null
  height?: number | null
  size_mb?: number
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options?: { retryNetwork?: number },
): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) }
  if (init?.body) headers['Content-Type'] = 'application/json'
  const retryNetwork = options?.retryNetwork ?? 0
  let response: Response | null = null
  for (let attempt = 0; ; attempt += 1) {
    try {
      response = await fetch(path, { credentials: 'same-origin', ...init, headers })
      break
    } catch {
      // 只有网络层失败会走到这里（服务重启、连接被重置）；给一次重试再报中文。
      if (attempt >= retryNetwork) {
        throw new Error('连接中断：服务可能正在重启或网络不可用，请稍后重试。')
      }
      await new Promise((resolve) => setTimeout(resolve, 400))
    }
  }
  let data: unknown = null
  try {
    data = await response.json()
  } catch {
    data = null
  }
  const payload = data as { ok?: boolean; error?: string } | null
  if (!response.ok || !payload || payload.ok === false) {
    throw new Error(payload?.error || `请求失败（HTTP ${response.status}）`)
  }
  return data as T
}

export function fetchBootstrap(): Promise<Bootstrap> {
  return request<Bootstrap>('/api/bootstrap')
}

export async function uploadImage(file: File): Promise<UploadResult> {
  if (file.size > 20 * 1024 * 1024) throw new Error('单张图片不能超过 20MB。')
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('读取图片失败，请重新选择。'))
    reader.readAsDataURL(file)
  })
  return request<UploadResult>('/api/uploads', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, data }),
  }, { retryNetwork: 1 })
}

// 生成结果的图片地址不是 upload，把它取回来再走一次上传，素材库才能当参考图复用。
export async function uploadFromUrl(url: string, filename: string): Promise<UploadResult> {
  let response: Response
  try {
    response = await fetch(url, { credentials: 'same-origin' })
  } catch {
    throw new Error('图片读取失败，请重试。')
  }
  if (!response.ok) throw new Error(`图片读取失败（HTTP ${response.status}）`)
  const blob = await response.blob()
  const type = blob.type.startsWith('image/') ? blob.type : 'image/png'
  const ext = type === 'image/jpeg' ? 'jpg' : (type.split('/')[1] || 'png')
  const hasExt = /\.[a-z0-9]+$/i.test(filename)
  const name = hasExt ? filename : `${filename || 'material'}.${ext}`
  return uploadImage(new File([blob], name, { type }))
}

export async function createImageTask(payload: {  prompt: string
  model: string
  size: string
  quality: string
  count: number
  upload_ids: string[]
}): Promise<ImageGenTask> {
  const data = await request<{ task: ImageGenTask }>('/api/imagegen/tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return data.task
}

export async function getImageTask(taskId: string): Promise<ImageGenTask> {
  const data = await request<{ task: ImageGenTask }>(`/api/imagegen/tasks/${taskId}`)
  return data.task
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return '操作失败，请重试。'
}
