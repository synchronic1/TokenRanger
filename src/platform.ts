/**
 * Cross-platform detection for service management.
 */

import os from "node:os";
import fs from "node:fs";
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
  return path.join(
    os.homedir(),
    "Library",
    "LaunchAgents",
    "com.openclaw.tokenranger.plist",
  );
}

export function resolveSystemdUnitDir(): string {
  return path.join(os.homedir(), ".config", "systemd", "user");
}
