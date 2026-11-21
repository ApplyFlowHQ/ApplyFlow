/** Typed API client with HttpOnly cookie auth, CSRF token attachment, and offline protection. */

function getCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : '';
}

let memoryCsrfToken = '';
export function setCsrfToken(token: string) {
  memoryCsrfToken = token || '';
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function apiRequest<T = any>(
  path: string,
  options: RequestInit & { idempotencyKey?: string } = {}
): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();
  if (method !== 'GET' && !navigator.onLine) {
    throw new ApiError(0, 'OFFLINE', 'Cannot perform this action while offline. Reconnect and try again.');
  }

  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = memoryCsrfToken || getCookie('applyflow_csrf');
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }
  if (options.idempotencyKey) {
    headers.set('Idempotency-Key', options.idempotencyKey);
  }

  const res = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: 'include',
    headers,
  });

  if (!res.ok) {
    let code = `HTTP_${res.status}`;
    let message = res.statusText || 'Request failed';
    try {
      const errJson = await res.json();
      if (errJson?.detail?.code) code = errJson.detail.code;
      if (errJson?.detail?.message) message = errJson.detail.message;
      else if (typeof errJson?.detail === 'string') message = errJson.detail;
    } catch {
      // ignore non-json error bodies
    }
    throw new ApiError(res.status, code, message);
  }

  const ctype = res.headers.get('content-type') || '';
  if (ctype.includes('application/json')) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}
