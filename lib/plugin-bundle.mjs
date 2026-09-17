import fs from "node:fs";
import path from "node:path";

const EXCLUDED_NAMES = new Set([
  ".agent-flow", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
  ".venv", "__pycache__", "node_modules", ".DS_Store",
]);
const PLUGIN_NAME = "agent-flow";
const ENTRY_PATH = "skills/agent-flow/SKILL.md";

function collectFiles(root, relative, files) {
  const absolute = path.join(root, relative);
  const stat = fs.lstatSync(absolute);
  if (stat.isSymbolicLink()) {
    throw new Error(`Plugin payload must not contain symlinks: ${relative}`);
  }
  if (stat.isDirectory()) {
    for (const name of fs.readdirSync(absolute).sort()) {
      if (!EXCLUDED_NAMES.has(name) && !name.endsWith(".pyc")) {
        collectFiles(root, path.join(relative, name), files);
      }
    }
  } else if (stat.isFile()) {
    files.set(relative, stat.mode & 0o111 ? 0o755 : 0o644);
  } else {
    throw new Error(`Plugin payload must contain only regular files: ${relative}`);
  }
}

function generatedEntry(canonical) {
  const frontmatter = canonical.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/);
  if (!frontmatter) throw new Error("Canonical agent-flow skill requires frontmatter");
  const preparation = `
## Native plugin project preparation

This plugin exposes only this entry. Its complete kit is bundled under \`kit/\`, outside native skill discovery. Enabling the plugin runs no installer or hooks.

Only when the user explicitly requests project preparation, resolve the installer for this installed copy:

- Claude Code substitutes \`\${CLAUDE_PLUGIN_ROOT}\` in plugin skills. The installer is \`\${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-flow-kit.mjs\`.
- Codex includes this skill's file path in its skill discovery context. Resolve \`../../kit/bin/agent-flow-kit.mjs\` relative to the directory containing that absolute \`SKILL.md\` path. Do not assume Claude's substitution or hook-only environment variables exist in the Codex shell. If the host has not supplied the skill path, report that missing prerequisite rather than guessing a cache location.

Resolve the target from the user's requested project and trusted checkout identity, independently of the plugin location. From that project's leader checkout, run \`node "<resolved-installer>" install --root "<confirmed-project-root>"\` with the explicitly selected profile and architecture options. Follow the installer's active-run and worktree guards; preparation never starts or resumes a run. This requires local Node and the Python dependencies reported by the installer; plugin enablement does not install them.

After preparation, follow the canonical entry's project-runtime selection before executing lifecycle commands. Verify project hook registration and host trust separately. Disabling/removing this plugin does not remove the project runtime, protection, run state, pins, or evidence. OMP uses its existing project adapter; Codex IDE plugin support is not claimed.

The canonical entry follows unchanged. Relative source references belong to \`../../kit/skills/agent-flow/\` from this skill directory; project-relative runtime paths still belong to the confirmed project.

`;
  return frontmatter[0] + preparation + canonical.slice(frontmatter[0].length);
}

function writeFile(root, relative, content, mode = 0o644) {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, { mode });
  fs.chmodSync(target, mode);
  fs.utimesSync(target, 0, 0);
}

function normalizeDirectories(root) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (entry.isDirectory()) normalizeDirectories(path.join(root, entry.name));
  }
  fs.chmodSync(root, 0o755);
  fs.utimesSync(root, 0, 0);
}

export function packPlugin(sourceRoot, outputDirectory) {
  const root = fs.realpathSync(sourceRoot);
  const output = path.resolve(outputDirectory);
  if (fs.existsSync(output)) throw new Error(`Output already exists: ${output}`);
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  if (!Array.isArray(pkg.files)) throw new Error("package.json.files must declare the kit payload");
  const files = new Map();
  for (const relative of ["package.json", ...pkg.files]) {
    if (typeof relative !== "string" || !relative || path.isAbsolute(relative)
        || relative.split(/[\\/]/).some((part) => part === ".." || part === ".")
        || /[*?\[\]{}!]/.test(relative)) {
      throw new Error(`Kit asset must be a literal relative path: ${relative}`);
    }
    const asset = path.join(root, relative);
    if (output === asset || output.startsWith(`${asset}${path.sep}`)) {
      throw new Error(`Output must be outside shipped asset trees: ${relative}`);
    }
    collectFiles(root, relative, files);
  }
  for (const required of [ENTRY_PATH, "skills/agent-flow/agents/openai.yaml", "bin/agent-flow-kit.mjs"]) {
    if (!files.has(required)) throw new Error(`Missing bundled entry dependency: ${required}`);
  }
  const entry = generatedEntry(fs.readFileSync(path.join(root, ENTRY_PATH), "utf8"));
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const temporary = fs.mkdtempSync(path.join(path.dirname(output), ".agent-flow-plugin-"));
  try {
    for (const [relative, mode] of [...files].sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)) {
      const content = fs.readFileSync(path.join(root, relative));
      writeFile(temporary, path.join("kit", relative), content, mode);
      if (relative.startsWith(`skills${path.sep}agent-flow${path.sep}`) && relative !== ENTRY_PATH) {
        writeFile(temporary, relative, content, mode);
      }
    }
    const identity = {
      name: PLUGIN_NAME,
      version: pkg.version,
      description: "Explicit Agent Flow entry with project-owned workflow runtime.",
    };
    writeFile(temporary, ".claude-plugin/plugin.json", `${JSON.stringify(identity, null, 2)}\n`);
    writeFile(temporary, "plugin.json", `${JSON.stringify({
      $schema: "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
      ...identity,
      extensions: { "com.openai": { interface: {
        displayName: "Agent Flow",
        shortDescription: identity.description,
      } } },
    }, null, 2)}\n`);
    writeFile(temporary, ENTRY_PATH, entry);
    normalizeDirectories(temporary);
    fs.renameSync(temporary, output);
  } catch (error) {
    fs.rmSync(temporary, { recursive: true, force: true });
    throw error;
  }
  return output;
}
