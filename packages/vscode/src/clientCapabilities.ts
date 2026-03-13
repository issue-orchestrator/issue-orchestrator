import type { ClientCapabilities } from "./types.js";

export const DEFAULT_CLIENT_CAPABILITIES: ClientCapabilities = {
  focus_session: false,
  open_path: false,
  reveal_worktree: false,
  local_server_paths_only: true,
  host_platform: "unknown",
};

export function normalizeClientCapabilities(
  capabilities?: Partial<ClientCapabilities> | null
): ClientCapabilities {
  return {
    ...DEFAULT_CLIENT_CAPABILITIES,
    ...(capabilities ?? {}),
  };
}

export function sessionActionMode(capabilities?: Partial<ClientCapabilities> | null): "focus" | "console" {
  return normalizeClientCapabilities(capabilities).focus_session ? "focus" : "console";
}
