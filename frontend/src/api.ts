export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public requestId?: string) {
    super(message)
  }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 60000)
  try {
    const response = await fetch('/api' + path, {
      credentials: 'same-origin', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'X-LLM4Web-Client': 'web' },
      ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }),
    })
    const data = await response.json()
    if (!response.ok) throw new ApiError(response.status, data.code || 'HTTP_ERROR', data.message || `请求失败 ${response.status}`, response.headers.get('X-Request-ID') || undefined)
    return data as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error('请求超时，请刷新执行记录确认实际状态。')
    throw error
  } finally { clearTimeout(timer) }
}
