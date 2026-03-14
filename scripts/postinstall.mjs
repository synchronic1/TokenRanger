/**
 * postinstall.mjs — run automatically after `npm install`.
 *
 * Creates node_modules/openclaw → ../../openclaw symlink so TypeScript can
 * resolve the plugin-sdk types without openclaw being published to npm.
 *
 * Looks for the openclaw package in order:
 *   1. OPENCLAW_SDK_PATH env var (explicit override)
 *   2. Sibling directory: <project-root>/../../openclaw
 *   3. ~/.openclaw/sdk (if openclaw installs its own SDK there)
 *   4. Skip silently — build will still work if openclaw is already linked
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "..");
const nodeModules = path.join(projectRoot, "node_modules");
const linkTarget = path.join(nodeModules, "openclaw");

function tryLink(candidatePath) {
  try {
    const real = fs.realpathSync(candidatePath);
    if (!fs.existsSync(path.join(real, "package.json"))) return false;

    // Remove existing link/dir if it points somewhere else
    const stat = fs.lstatSync(linkTarget, { throwIfNoEntry: false });
    if (stat) {
      try {
        const existing = fs.readlinkSync(linkTarget);
        if (existing === real || path.resolve(path.dirname(linkTarget), existing) === real) {
          console.log(`[tokenranger] openclaw symlink already correct → ${real}`);
          return true;
        }
      } catch { /* not a symlink, remove it */ }
      fs.rmSync(linkTarget, { recursive: true, force: true });
    }

    fs.symlinkSync(real, linkTarget, "dir");
    console.log(`[tokenranger] linked node_modules/openclaw → ${real}`);
    return true;
  } catch {
    return false;
  }
}

const candidates = [
  process.env.OPENCLAW_SDK_PATH,
  path.resolve(projectRoot, "..", "..", "openclaw"),
  path.resolve(projectRoot, "..", "openclaw"),
  path.join(os.homedir(), ".openclaw", "sdk"),
].filter(Boolean);

fs.mkdirSync(nodeModules, { recursive: true });

// If link already exists and resolves, keep it
try {
  if (fs.existsSync(linkTarget)) {
    fs.realpathSync(linkTarget); // throws if broken
    console.log(`[tokenranger] openclaw symlink present, skipping`);
    process.exit(0);
  }
} catch {
  fs.rmSync(linkTarget, { recursive: true, force: true });
}

let linked = false;
for (const candidate of candidates) {
  if (tryLink(candidate)) {
    linked = true;
    break;
  }
}

if (!linked) {
  console.warn(
    "[tokenranger] openclaw package not found — TypeScript build may fail.\n" +
    "  Set OPENCLAW_SDK_PATH=/path/to/openclaw and re-run npm install, or\n" +
    "  place the openclaw repo as a sibling directory.",
  );
}
