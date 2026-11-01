export class ApiError extends Error {}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = sessionStorage.getItem("kubeml.token");
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  const body = (await response.json().catch(() => ({}))) as { error?: string; detail?: string };
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : body.error;
    throw new ApiError(detail || `Request failed (${response.status}).`);
  }
  return body as T;
}
