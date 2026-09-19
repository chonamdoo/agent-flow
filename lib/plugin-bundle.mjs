import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const EXCLUDED_NAMES = new Set([
  ".agent-flow", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
  ".venv", "__pycache__", "node_modules", ".DS_Store",
]);
const PLUGIN_NAME = "agent-flow";
const ENTRY_PATH = "skills/agent-flow/SKILL.md";
const INCOMPLETE_MARKER = ".agent-flow-plugin-incomplete";

function isInterruptedPack(output, stat) {
  return stat.isDirectory() && !stat.isSymbolicLink()
    && fs.lstatSync(path.join(output, INCOMPLETE_MARKER), { throwIfNoEntry: false })?.isFile() === true;
}

const NOFOLLOW_FLAGS = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);

function sameInode(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

// lstat로 검사한 경로를 나중에 다시 열면 그 사이 symlink로 바꿔치기된 외부 파일이
// payload에 들어간다. 내용은 검사 직후 no-follow fd로 한 번에 읽고, 읽은 fd와
// 읽기 뒤의 경로가 검사 때와 같은 inode이며 realpath가 자기 자신인지 다시 확인한다.
function readPayloadFile(absolute, relative, stat) {
  const fd = fs.openSync(absolute, NOFOLLOW_FLAGS);
  let content;
  try {
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || !sameInode(opened, stat)) {
      throw new Error(`Plugin payload changed during packing: ${relative}`);
    }
    content = fs.readFileSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  if (!sameInode(fs.lstatSync(absolute), stat) || fs.realpathSync(absolute) !== absolute) {
    throw new Error(`Plugin payload changed during packing: ${relative}`);
  }
  return content;
}

function collectFiles(root, relative, files) {
  const absolute = path.join(root, relative);
  const stat = fs.lstatSync(absolute);
  if (stat.isSymbolicLink()) {
    throw new Error(`Plugin payload must not contain symlinks: ${relative}`);
  }
  if (stat.isDirectory()) {
    const names = fs.readdirSync(absolute).sort();
    if (!sameInode(fs.lstatSync(absolute), stat) || fs.realpathSync(absolute) !== absolute) {
      throw new Error(`Plugin payload changed during packing: ${relative}`);
    }
    for (const name of names) {
      if (!EXCLUDED_NAMES.has(name) && !name.endsWith(".pyc")) {
        collectFiles(root, path.posix.join(relative, name), files);
      }
    }
  } else if (stat.isFile()) {
    files.set(relative, {
      mode: stat.mode & 0o111 ? 0o755 : 0o644,
      content: readPayloadFile(absolute, relative, stat),
    });
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

// 차지한 출력 디렉터리나 그 아래 중간 디렉터리가 symlink로 바뀌면 경로 기반 쓰기와
// 정리가 남의 트리로 간다. 매 쓰기·정리 전후에 출력 루트가 우리가 만든 inode이고 대상
// 경로의 realpath가 자기 자신(모든 구성요소가 실제 디렉터리)인지 본다. 파일은 새 출력
// 안에서만 생기므로 O_EXCL|O_NOFOLLOW로 만들어 기존 항목이나 링크 위에는 쓰지 않는다.
function assertClaimed(output, claimed, target = output) {
  const current = fs.lstatSync(output, { throwIfNoEntry: false });
  if (!current || !current.isDirectory() || !sameInode(current, claimed)
      || fs.realpathSync(target) !== target) {
    throw new Error(`Output changed during packing: ${target}`);
  }
}

const NEW_FILE_FLAGS = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
  | (fs.constants.O_NOFOLLOW ?? 0);

// 이 호출이 만든 항목만 inode와 함께 `owned`에 적는다. 마무리 때 출력 안의 모든 항목이
// 그 inode여야 하고, 실패 정리는 그 inode인 항목만 지운다 — 그 사이 남이 넣거나 바꾼
// 것은 배포물에 넣지도, 지우지도 않는다.
function writeFile(root, relative, content, mode, claimed, owned) {
  const target = path.join(root, relative);
  const directory = path.dirname(target);
  assertClaimed(root, claimed);
  for (let parent = directory, missing = []; ; parent = path.dirname(parent)) {
    if (parent === root || fs.lstatSync(parent, { throwIfNoEntry: false })) {
      for (const created of missing.reverse()) {
        fs.mkdirSync(created);
        owned.set(created, fs.lstatSync(created));
      }
      break;
    }
    missing.push(parent);
  }
  assertClaimed(root, claimed, directory);
  const fd = fs.openSync(target, NEW_FILE_FLAGS, mode);
  try {
    owned.set(target, fs.fstatSync(fd));
    fs.writeFileSync(fd, content);
    fs.fchmodSync(fd, mode);
    fs.futimesSync(fd, 0, 0);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  assertClaimed(root, claimed, target);
}

// marker를 지우기 전에 payload와 디렉터리 항목이 디스크에 있어야 한다. 그렇지 않으면
// 전원이 끊긴 뒤 marker 삭제만 남아 잘린 출력이 완료된 것으로 보인다. 디렉터리
// fsync를 지원하지 않는 곳(Windows, 일부 파일시스템)의 오류만 삼키고, EIO 같은 실제
// 실패는 올려서 완료로 보고하지 않는다.
const UNSUPPORTED_DIRECTORY_SYNC = new Set(["EINVAL", "EISDIR", "EPERM", "ENOTSUP"]);

function fsyncDirectory(directory) {
  let handle = null;
  try {
    handle = fs.openSync(directory, "r");
    fs.fsyncSync(handle);
  } catch (error) {
    if (!UNSUPPORTED_DIRECTORY_SYNC.has(error.code)) throw error;
  } finally {
    if (handle !== null) fs.closeSync(handle);
  }
}

function ensureParentDirectories(ancestor, target) {
  let parent = ancestor;
  const relative = path.relative(ancestor, target);
  for (const part of relative ? relative.split(path.sep) : []) {
    const directory = path.join(parent, part);
    try {
      fs.mkdirSync(directory);
    } catch (error) {
      const current = fs.lstatSync(directory, { throwIfNoEntry: false });
      if (error.code !== "EEXIST" || !current?.isDirectory() || current.isSymbolicLink()
          || fs.realpathSync(directory) !== directory) {
        throw error;
      }
    }
    fsyncDirectory(parent);
    parent = directory;
  }
}

function isOwned(owned, target, current) {
  const recorded = owned.get(target);
  return recorded !== undefined && sameInode(recorded, current)
    && current.isDirectory() === recorded.isDirectory() && !current.isSymbolicLink();
}

function removeOwned(output, claimed, owned) {
  if (!sameInode(fs.lstatSync(output, { throwIfNoEntry: false }) ?? {}, claimed)) return;
  const entries = [...owned.keys()].sort((left, right) => right.length - left.length);
  for (const target of entries.concat(output)) {
    const current = fs.lstatSync(target, { throwIfNoEntry: false });
    if (!current) continue;
    if (target !== output && !isOwned(owned, target, current)) {
      console.error(`failed plugin output retained with foreign entries: ${target}`);
      return;
    }
    if (current.isFile()) {
      fs.unlinkSync(target);
    } else if (fs.readdirSync(target).length === 0) {
      fs.rmdirSync(target);
    } else {
      console.error(`failed plugin output retained with foreign entries: ${target}`);
      return;
    }
  }
}

function finalizeDirectories(root, claimed, owned, directory = root) {
  assertClaimed(root, claimed, directory);
  for (const name of fs.readdirSync(directory)) {
    const entry = path.join(directory, name);
    if (!isOwned(owned, entry, fs.lstatSync(entry))) {
      throw new Error(`Output changed during packing: ${entry}`);
    }
    if (owned.get(entry).isDirectory()) finalizeDirectories(root, claimed, owned, entry);
  }
  assertClaimed(root, claimed, directory);
  fs.chmodSync(directory, 0o755);
  fs.utimesSync(directory, 0, 0);
}

export function packPlugin(sourceRoot, outputDirectory) {
  const root = fs.realpathSync(sourceRoot);
  const requestedOutput = path.resolve(outputDirectory);
  let ancestor = requestedOutput;
  while (!fs.lstatSync(ancestor, { throwIfNoEntry: false })) {
    ancestor = path.dirname(ancestor);
  }
  const output = path.resolve(fs.realpathSync(ancestor), path.relative(ancestor, requestedOutput));
  const existing = fs.lstatSync(output, { throwIfNoEntry: false });
  if (existing && !isInterruptedPack(output, existing)) throw new Error(`Output already exists: ${output}`);
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  if (!Array.isArray(pkg.files)) throw new Error("package.json.files must declare the kit payload");
  const assets = ["package.json", ...pkg.files];
  for (const relative of assets) {
    if (typeof relative !== "string" || !relative || path.isAbsolute(relative) || relative.includes("\\")
        || relative.split("/").some((part) => part === ".." || part === ".")
        || /[*?\[\]{}!]/.test(relative)) {
      throw new Error(`Kit asset must be a literal relative path: ${relative}`);
    }
    const asset = path.join(root, relative);
    if (fs.realpathSync(asset) !== asset) {
      throw new Error(`Plugin payload must not contain symlinks: ${relative}`);
    }
    if (output === asset || output.startsWith(`${asset}${path.sep}`)) {
      throw new Error(`Output must be outside shipped asset trees: ${relative}`);
    }
  }
  // 존재 확인 뒤 rename하면 그 사이 남이 만든 빈 디렉터리를 덮어쓰고, Windows는 디렉터리
  // 위로 rename하지 못한다. mkdir로 자리를 배타적으로 차지한 뒤 그 안에 바로 쓴다. 우리
  // marker가 남은 중단 출력은 지우지 않고 옆으로 옮겨 둔다 — marker는 공개 이름이라 그
  // 안에 사용자 파일이 있을 수 있고, rename은 배타적이라 두 packer가 같은 자리를 함께
  // 복구하지 못한다.
  ensureParentDirectories(ancestor, path.dirname(output));
  if (existing) {
    const aside = `${output}.interrupted-${crypto.randomBytes(4).toString("hex")}`;
    try {
      fs.renameSync(output, aside);
    } catch (error) {
      throw error.code === "ENOENT" ? new Error(`Output already exists: ${output}`) : error;
    }
    console.error(`interrupted plugin output preserved: ${aside}`);
  }
  try {
    fs.mkdirSync(output);
  } catch (error) {
    throw error.code === "EEXIST" ? new Error(`Output already exists: ${output}`) : error;
  }
  const claimed = fs.lstatSync(output);
  const files = new Map();
  const owned = new Map();
  try {
    writeFile(output, INCOMPLETE_MARKER, "", 0o644, claimed, owned);
    fsyncDirectory(output);
    fsyncDirectory(path.dirname(output));
    for (const relative of assets) collectFiles(root, relative, files);
    for (const required of [ENTRY_PATH, "skills/agent-flow/agents/openai.yaml", "bin/agent-flow-kit.mjs"]) {
      if (!files.has(required)) throw new Error(`Missing bundled entry dependency: ${required}`);
    }
    const entry = generatedEntry(files.get(ENTRY_PATH).content.toString("utf8"));
    for (const [relative, { mode, content }] of [...files].sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)) {
      writeFile(output, path.join("kit", relative), content, mode, claimed, owned);
      if (relative.startsWith("skills/agent-flow/") && relative !== ENTRY_PATH) {
        writeFile(output, relative, content, mode, claimed, owned);
      }
    }
    const identity = {
      name: PLUGIN_NAME,
      version: pkg.version,
      description: "Explicit Agent Flow entry with project-owned workflow runtime.",
    };
    writeFile(output, ".claude-plugin/plugin.json", `${JSON.stringify(identity, null, 2)}\n`, 0o644, claimed, owned);
    writeFile(output, "plugin.json", `${JSON.stringify({
      $schema: "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
      ...identity,
      extensions: { "com.openai": { interface: {
        displayName: "Agent Flow",
        shortDescription: identity.description,
      } } },
    }, null, 2)}\n`, 0o644, claimed, owned);
    writeFile(output, ENTRY_PATH, entry, 0o644, claimed, owned);
    finalizeDirectories(output, claimed, owned);
    for (const [target, stat] of owned) if (stat.isDirectory()) fsyncDirectory(target);
    fsyncDirectory(output);
    fsyncDirectory(path.dirname(output));
    assertClaimed(output, claimed);
    fs.unlinkSync(path.join(output, INCOMPLETE_MARKER));
    fs.utimesSync(output, 0, 0);
    fsyncDirectory(output);
  } catch (error) {
    removeOwned(output, claimed, owned);
    throw error;
  }
  return output;
}
