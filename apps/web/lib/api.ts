import { bearerToken } from "./auth";

export async function apiFetch(path: string, init: RequestInit = {}) {
  if (!path.startsWith("/v1/")) throw new Error("API path ต้องอยู่ใต้ /v1/");
  const token = await bearerToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(path, { ...init, headers });
}

export async function apiJson<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) throw new Error(response.status === 401 ? "กรุณาเข้าสู่ระบบก่อน" : `API ${response.status}`);
  return response.status === 204 ? null as T : response.json();
}
