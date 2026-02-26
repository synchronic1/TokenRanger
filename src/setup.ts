/**
 * CLI setup command implementation.
 * Installs the Python compression service, pulls Ollama models,
 * and creates a system service (launchd or systemd).
 */

import { execSync, spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  detectPlatform,
  resolveServiceDir,
  resolveLaunchdPlistPath,
  resolveSystemdUnitDir,
} from "./platform.js";
import { checkServiceHealth } from "./health.js";
import type { TokenRangerConfig } from "./config.js";

type Logger = {
  info: (msg: string) => void;
  warn: (msg: string) => void;
  error: (msg: string) => void;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeXml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function venvBin(serviceDir: string, name: string): string {
  const subdir = process.platform === "win32" ? "Scripts" : "bin";
  const ext = process.platform === "win32" ? ".exe" : "";
  return path.join(serviceDir, "venv", subdir, `${name}${ext}`);
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default model pulled on first setup. Matches config.ts DEFAULTS. */
const DEFAULT_GPU_MODEL = "mistral:7b-instruct";
const DEFAULT_CPU_MODEL = "phi3.5:latest";

// ---------------------------------------------------------------------------
// Prerequisite checks
// ---------------------------------------------------------------------------

function checkCommand(cmd: string): boolean {
  const whichCmd = process.platform === "win32" ? "where" : "which";
  const result = spawnSync(whichCmd, [cmd], { stdio: "ignore" });
  return result.status === 0;
}

function checkPythonVersion(): string | null {
  try {
    const out = execSync("python3 --version", { encoding: "utf-8" }).trim();
    // "Python 3.10.12" -> "3.10.12"
    const ver = out.replace("Python ", "");
    const [major, minor] = ver.split(".").map(Number);
    if (major >= 3 && minor >= 10) return ver;
    return null;
  } catch {
    return null;
  }
}

export function checkPrerequisites(logger: Logger): boolean {
  let ok = true;

  const pyVer = checkPythonVersion();
  if (pyVer) {
    logger.info(`  python3 ${pyVer} ✓`);
  } else {
    logger.error("  python3 >= 3.10 not found ✗");
    ok = false;
  }

  if (checkCommand("pip3") || checkCommand("pip")) {
    logger.info("  pip ✓");
  } else {
    logger.error("  pip not found ✗");
    ok = false;
  }

  if (checkCommand("ollama")) {
    logger.info("  ollama ✓");
  } else {
    logger.warn("  ollama not found — will attempt to install");
  }

  return ok;
}

// ---------------------------------------------------------------------------
// Ollama installation
// ---------------------------------------------------------------------------

export function installOllama(logger: Logger): boolean {
  if (checkCommand("ollama")) {
    logger.info("  ollama already installed ✓");
    return true;
  }

  const platform = process.platform;

  if (platform === "linux") {
    logger.info("  installing Ollama via install script...");
    const result = spawnSync("sh", ["-c", "curl -fsSL https://ollama.com/install.sh | sh"], {
      stdio: "inherit",
      timeout: 120_000,
    });
    if (result.status === 0 && checkCommand("ollama")) {
      logger.info("  ollama installed ✓");
      return true;
    }
    logger.error("  ollama install failed — install manually: https://ollama.com/download");
    return false;
  }

  if (platform === "darwin") {
    // Try Homebrew first
    if (checkCommand("brew")) {
      logger.info("  installing Ollama via Homebrew...");
      const result = spawnSync("brew", ["install", "ollama"], {
        stdio: "inherit",
        timeout: 120_000,
      });
      if (result.status === 0 && checkCommand("ollama")) {
        logger.info("  ollama installed ✓");
        return true;
      }
    }
    logger.error("  ollama not found — install from: https://ollama.com/download");
    return false;
  }

  logger.error("  ollama not found — install from: https://ollama.com/download");
  return false;
}

// ---------------------------------------------------------------------------
// Python service installation
// ---------------------------------------------------------------------------

export function installPythonService(
  serviceDir: string,
  pluginDir: string,
  logger: Logger,
): void {
  // Create service directory
  fs.mkdirSync(serviceDir, { recursive: true });

  // Copy Python files from plugin's service/ directory
  const sourceDir = path.join(pluginDir, "service");
  const files = ["main.py", "config.py", "inference_router.py", "compressor.py", "requirements.txt"];

  for (const file of files) {
    const src = path.join(sourceDir, file);
    const dst = path.join(serviceDir, file);
    if (fs.existsSync(src)) {
      fs.copyFileSync(src, dst);
      logger.info(`  copied ${file}`);
    } else {
      logger.warn(`  ${file} not found in plugin directory`);
    }
  }

  // Create venv
  logger.info("  creating Python venv...");
  const venvResult = spawnSync("python3", ["-m", "venv", "venv"], {
    cwd: serviceDir,
    stdio: "inherit",
    timeout: 60_000,
  });
  if (venvResult.status !== 0) {
    throw new Error("Failed to create Python venv");
  }

  // Install dependencies
  logger.info("  installing Python dependencies...");
  const pipBin = venvBin(serviceDir, "pip");
  const reqFile = path.join(serviceDir, "requirements.txt");
  const pipResult = spawnSync(pipBin, ["install", "-r", reqFile], {
    cwd: serviceDir,
    stdio: "inherit",
    timeout: 300_000,
  });
  if (pipResult.status !== 0) {
    throw new Error("Failed to install Python dependencies");
  }

  logger.info("  Python service installed ✓");
}

// ---------------------------------------------------------------------------
// Ollama model pull
// ---------------------------------------------------------------------------

export function ensureOllamaRunning(logger: Logger): void {
  if (!checkCommand("ollama")) return;

  // Check if Ollama is already serving
  try {
    const result = spawnSync("ollama", ["list"], { timeout: 5_000, encoding: "utf-8" });
    if (result.status === 0) return; // already running
  } catch {
    // not running
  }

  logger.info("  starting Ollama...");
  const child = spawn("ollama", ["serve"], {
    stdio: "ignore",
    detached: true,
  });
  child.unref();
  // Give it a moment to bind
  spawnSync("sleep", ["2"]);
}

export function pullOllamaModel(model: string, ollamaUrl: string, logger: Logger): void {
  if (!checkCommand("ollama")) {
    logger.warn("  ollama not found, skipping model pull");
    return;
  }

  ensureOllamaRunning(logger);

  // Resolve model — use default if empty or generic
  const resolvedModel = model && model !== "mistral:7b"
    ? model
    : DEFAULT_GPU_MODEL;

  // Check if model already exists
  try {
    const list = execSync("ollama list", { encoding: "utf-8" });
    if (list.includes(resolvedModel)) {
      logger.info(`  model ${resolvedModel} already present ✓`);
      return;
    }
  } catch {
    // ignore
  }

  logger.info(`  pulling ${resolvedModel} (this may take a few minutes)...`);
  const result = spawnSync("ollama", ["pull", resolvedModel], {
    stdio: "inherit",
    timeout: 600_000,
  });
  if (result.status !== 0) {
    logger.warn(`  model pull failed — compression will fall back to passthrough`);
  } else {
    logger.info(`  model ${resolvedModel} pulled ✓`);
  }
}

// ---------------------------------------------------------------------------
// System service installation
// ---------------------------------------------------------------------------

export function installLaunchdService(
  serviceDir: string,
  pluginDir: string,
  config: TokenRangerConfig,
  logger: Logger,
): void {
  const uvicornBin = venvBin(serviceDir, "uvicorn");
  const logDir = path.join(os.homedir(), ".openclaw", "logs");
  fs.mkdirSync(logDir, { recursive: true });

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openclaw.tokenranger</string>
    <key>ProgramArguments</key>
    <array>
        <string>${uvicornBin}</string>
        <string>main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8100</string>
        <string>--workers</string>
        <string>1</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${serviceDir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${logDir}/tokenranger.log</string>
    <key>StandardErrorPath</key>
    <string>${logDir}/tokenranger.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TOKENRANGER_OLLAMA_BASE_URL</key>
        <string>${escapeXml(config.ollamaUrl)}</string>
    </dict>
</dict>
</plist>`;

  const plistPath = resolveLaunchdPlistPath();
  fs.writeFileSync(plistPath, plist);
  logger.info(`  wrote ${plistPath}`);

  // Load the service (use spawnSync to avoid shell injection on paths)
  try {
    spawnSync("launchctl", ["unload", plistPath], { stdio: "ignore" });
    const loadResult = spawnSync("launchctl", ["load", plistPath], { stdio: "inherit" });
    if (loadResult.status === 0) {
      logger.info("  launchd service loaded ✓");
    } else {
      logger.warn(`  launchctl load exited with code ${loadResult.status}`);
    }
  } catch (err) {
    logger.warn(`  launchctl load failed: ${String(err)}`);
  }
}

export function installSystemdService(
  serviceDir: string,
  pluginDir: string,
  config: TokenRangerConfig,
  logger: Logger,
): void {
  const uvicornBin = venvBin(serviceDir, "uvicorn");
  const logDir = path.join(os.homedir(), ".openclaw", "logs");
  fs.mkdirSync(logDir, { recursive: true });

  const unit = `[Unit]
Description=OpenClaw TokenRanger Compression Service
After=ollama.service

[Service]
Type=simple
WorkingDirectory=${serviceDir}
ExecStart=${uvicornBin} main:app --host 127.0.0.1 --port 8100 --workers 1
Environment="TOKENRANGER_OLLAMA_BASE_URL=${config.ollamaUrl}"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
`;

  const unitDir = resolveSystemdUnitDir();
  fs.mkdirSync(unitDir, { recursive: true });
  const unitPath = path.join(unitDir, "openclaw-tokenranger.service");
  fs.writeFileSync(unitPath, unit);
  logger.info(`  wrote ${unitPath}`);

  try {
    execSync("systemctl --user daemon-reload");
    execSync("systemctl --user enable --now openclaw-tokenranger.service");
    logger.info("  systemd service enabled and started ✓");
  } catch (err) {
    logger.warn(`  systemctl failed: ${String(err)}`);
    logger.info(`  manual start: ${uvicornBin} main:app --host 127.0.0.1 --port 8100`);
  }
}

// ---------------------------------------------------------------------------
// Uninstall
// ---------------------------------------------------------------------------

export function uninstallService(logger: Logger): void {
  const platform = detectPlatform();
  const serviceDir = resolveServiceDir();

  if (platform.serviceManager === "launchd") {
    const plistPath = resolveLaunchdPlistPath();
    try {
      spawnSync("launchctl", ["unload", plistPath], { stdio: "ignore" });
      if (fs.existsSync(plistPath)) fs.unlinkSync(plistPath);
      logger.info("  launchd service removed ✓");
    } catch {
      // ignore
    }
  } else if (platform.serviceManager === "systemd") {
    try {
      execSync("systemctl --user disable --now openclaw-tokenranger.service 2>/dev/null || true");
      const unitPath = path.join(resolveSystemdUnitDir(), "openclaw-tokenranger.service");
      if (fs.existsSync(unitPath)) fs.unlinkSync(unitPath);
      execSync("systemctl --user daemon-reload");
      logger.info("  systemd service removed ✓");
    } catch {
      // ignore
    }
  }

  if (fs.existsSync(serviceDir)) {
    fs.rmSync(serviceDir, { recursive: true, force: true });
    logger.info(`  removed ${serviceDir} ✓`);
  }

  logger.info("  uninstall complete");
}

// ---------------------------------------------------------------------------
// Verification
// ---------------------------------------------------------------------------

export async function verifySetup(
  serviceUrl: string,
  logger: Logger,
): Promise<boolean> {
  // Give the service a moment to start
  await new Promise((resolve) => setTimeout(resolve, 2000));

  const health = await checkServiceHealth(serviceUrl, 5000);

  if (health.status === "healthy") {
    logger.info(`  service healthy: strategy=${health.strategy}, model=${health.model} ✓`);
    return true;
  }

  if (health.status === "degraded") {
    logger.warn(`  service degraded (Ollama may not be running)`);
    return true;
  }

  logger.error(`  service unreachable at ${serviceUrl}`);
  logger.info("  check logs: ~/.openclaw/logs/tokenranger.log");
  return false;
}
