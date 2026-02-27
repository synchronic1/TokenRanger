/**
 * Cross-platform detection for service management.
 *
 * Supported: Linux (systemd), macOS (launchd).
 * Windows: Use WSL2 — the extension runs as a Linux service inside WSL.
 * Native Windows is not a target platform.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export type Platform = {
  os: "darwin" | "linux" | "other";
  serviceManager: "launchd" | "systemd" | "none";
};

export function detectPlatform(): Platform {
  const platform = os.platform();

  if (platform === "darwin") {
    return { os: "darwin", serviceManager: "launchd" };
  }

  if (platform === "linux") {
    try {
      if (fs.existsSync("/run/systemd/system")) {
        return { os: "linux", serviceManager: "systemd" };
      }
    } catch {
      // ignore
    }
    return { os: "linux", serviceManager: "none" };
  }

  return { os: "other", serviceManager: "none" };
}

export function resolveServiceDir(): string {
  return path.join(os.homedir(), ".openclaw", "services", "tokenranger");
}

export function resolveLaunchdPlistPath(): string {
  return path.join(os.homedir(), "Library", "LaunchAgents", "com.openclaw.tokenranger.plist");
}

export function resolveSystemdUnitDir(): string {
  return path.join(os.homedir(), ".config", "systemd", "user");
}
