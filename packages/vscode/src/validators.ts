import type { Snapshot, StatusPayload, InfoPayload } from "./types.js";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isStatusPayload(value: unknown): value is StatusPayload {
  if (!isObject(value)) {
    return false;
  }
  return Array.isArray((value as StatusPayload).active_sessions) && Array.isArray((value as StatusPayload).queue);
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
  return isStatusPayload((value as Snapshot).status) && isInfoPayload((value as Snapshot).info);
}
