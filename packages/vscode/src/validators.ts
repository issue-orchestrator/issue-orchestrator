import type { Snapshot, StatusPayload, InfoPayload } from "./types.js";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isStatusPayload(value: unknown): value is StatusPayload {
  if (!isObject(value)) {
    return false;
  }
  const candidate = value as unknown as StatusPayload;
  return Array.isArray(candidate.active_sessions) && Array.isArray(candidate.queue);
}

export function isInfoPayload(value: unknown): value is InfoPayload {
  if (!isObject(value)) {
    return false;
  }
  return "repo" in value && "repo_root" in value;
}

export function isSnapshot(value: unknown): value is Snapshot {
  if (!isObject(value)) {
    return false;
  }
  const candidate = value as unknown as Snapshot;
  return isStatusPayload(candidate.status) && isInfoPayload(candidate.info);
}
