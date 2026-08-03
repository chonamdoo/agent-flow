// 두 install 진입점이 공유하는 헬퍼. 모듈 상태 대신 경로와 옵션을 인자로 받아
// 한쪽 진입점만 다른 보안·보존 정책을 갖지 않게 한다.

// `hooksDisabled`와 `force`는 진입점마다 정해지므로 기본값을 두지 않는다. 인자를
// 빠뜨린 호출이 hook을 켜거나 사용자 파일을 덮는 쪽으로 조용히 처리되면 안 된다.

import crypto from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MANAGED_HOOK_POLICY_SEQUENCES,
  MANAGED_HOOK_SCRIPTS,
  RETIRED_MANAGED_HOOK_SCRIPTS,
} from "./managed-hooks.mjs";
import {
  OMP_EXTENSION_MARKER,
  ompHooksExtensionSource,
} from "./omp-hooks-extension.mjs";
import {
  agentFlowHome,
  recordOmpAdapter,
  sharedHookLauncherInvocation,
  withSharedHookMutation,
} from "./shared-hook-runtime.mjs";
import { SKILL_DEPENDENCIES } from "./skill-selection.mjs";

const OMP_EXTENSION_MARKER_PREFIX = `// ${OMP_EXTENSION_MARKER}\n`;
const LEGACY_OMP_EXTENSION_DIGESTS = new Set([
  "7e70b38f3e1c4dff4c4f1a332b5722c51650950b1ce3cfe2349cdf89fd057fab",
  "fbadd85779b310d2f167678efaa46178b707a07652ec9aaa6518624a4db7814a",
  "82cb954fdbfa5a7555811eb0c8928c51446d556c6b0d38b3ac4fc464a6e59fa2",
  "be7c56d960720ac3589c0b3150b249e4e1219e8be138ca36cb59060f9ec945e7",
  "bee7a36d235c5310c7c627c89942977395590fde67b274fa330ddafa7f68e527",
]);

const RETIRE_OMP_EXTENSION_SCRIPT = String.raw`
import hashlib
import json
import os
import secrets
import stat
import sys

root, marker, legacy_json = sys.argv[1:]
legacy = set(json.loads(legacy_json))
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
dir_flags = flags | getattr(os, "O_DIRECTORY", 0)
name = "agent-flow-hooks.ts"
quarantine = None

def emit(status, **details):
    print(json.dumps({"status": status, **details}))

def owned(payload):
    return payload.startswith(marker.encode()) or hashlib.sha256(payload).hexdigest() in legacy

def read_at(parent, entry):
    try:
        handle = os.open(entry, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as stream:
        identity = os.fstat(stream.fileno())
        payload = stream.read()
    if not stat.S_ISREG(identity.st_mode):
        raise OSError(f"{entry} is not a regular file")
    return payload, (identity.st_dev, identity.st_ino)

def restore(parent, entry, retired):
    os.link(
        retired,
        entry,
        src_dir_fd=parent,
        dst_dir_fd=parent,
        follow_symlinks=False,
    )
    os.unlink(retired, dir_fd=parent)

descriptor = None
try:
    descriptor = os.open(root, dir_flags)
    for component in (".omp", "extensions"):
        try:
            child = os.open(component, dir_flags, dir_fd=descriptor)
        except FileNotFoundError:
            emit("missing")
            raise SystemExit(0)
        os.close(descriptor)
        descriptor = child

    observed = read_at(descriptor, name)
    if observed is None:
        emit("missing")
        raise SystemExit(0)
    payload, identity = observed
    if not owned(payload):
        emit("user-owned")
        raise SystemExit(0)

    quarantine = f"{name}.agent-flow-retired.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        os.rename(name, quarantine, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    except FileNotFoundError:
        emit("changed")
        raise SystemExit(0)

    moved = read_at(descriptor, quarantine)
    if moved is None or moved[1] != identity or moved[0] != payload or not owned(moved[0]):
        if moved is not None:
            restore(descriptor, name, quarantine)
            quarantine = None
        emit("changed")
        raise SystemExit(0)

    backup = None
    for index in range(100):
        candidate = f"{name}.removed" if index == 0 else f"{name}.removed.{index}"
        existing = read_at(descriptor, candidate)
        if existing is not None:
            if existing[0] == payload:
                break
            continue
        try:
            backup_handle = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
                dir_fd=descriptor,
            )
        except FileExistsError:
            continue
        with os.fdopen(backup_handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        backup = candidate
        break
    else:
        restore(descriptor, name, quarantine)
        quarantine = None
        emit("unsafe", detail="no safe backup name is available")
        raise SystemExit(0)

    current = read_at(descriptor, quarantine)
    if current is None or current[1] != identity or current[0] != payload or not owned(current[0]):
        if current is not None:
            restore(descriptor, name, quarantine)
            quarantine = None
        emit("changed")
        raise SystemExit(0)
    os.unlink(quarantine, dir_fd=descriptor)
    quarantine = None
    emit("removed", backup=backup)
except SystemExit:
    raise
except BaseException as exc:
    if descriptor is not None and quarantine is not None:
        try:
            restore(descriptor, name, quarantine)
            quarantine = None
        except BaseException:
            pass
    emit("unsafe", detail=str(exc))
finally:
    if descriptor is not None:
        os.close(descriptor)
`;

// prune 알림의 접두사. `agent-flow-install.mjs`가 자식 kit install의 stdout에서
// 이 접두사로 시작하는 줄만 되살리므로, 사용자에게 보여야 하는 prune 알림은
// 반드시 이 접두사를 써야 한다.
export const PRUNE_NOTICE_PREFIX = "  - pruned: ";

const ROOT_OPTION = "--root";

// 단일값 옵션 스캔. `agent-flow-kit.mjs`가 제 사본을 들고 있었고, install 진입점이
// 둘이라 세 번째 사본이 생길 자리였다.
export function extractCliOption(args, name) {
  const kept = [];
  let value;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === name) {
      if (index + 1 >= args.length) {
        throw new Error(`${name} requires a value`);
      }
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = args[index + 1];
      index += 1;
    } else if (arg.startsWith(`${name}=`)) {
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = arg.slice(name.length + 1);
    } else {
      kept.push(arg);
    }
  }
  return { value, args: kept };
}

export function cliOptionValue(args, name) {
  return extractCliOption(args, name).value;
}

// `--root`가 없으면 undefined다. cwd로 접어서 돌려주면 호출자가 "사용자가 지정한
// 경로"와 "그냥 cwd"를 구분하지 못해, 지정한 경로가 다른 곳으로 옮겨지는 것을
// 잡아낼 수 없다.
export function requestedInstallRootOption(args, cwd) {
  const requested = cliOptionValue(args, ROOT_OPTION);
  if (requested === undefined) {
    return undefined;
  }
  // 빈 값은 cwd로 접혀 원래 버그와 같은 결과를 내고, `-`로 시작하는 값은 다음
  // 플래그를 root로 삼킨다 — 그 이름의 디렉터리가 있으면 실제로 거기 설치된다.
  if (!requested || requested.startsWith("-")) {
    throw new Error(`${ROOT_OPTION} requires a value`);
  }
  const resolved = path.resolve(cwd, requested);
  // install은 없는 경로를 mkdir로 만들어 낸다. 오타 하나가 빈 트리를 심고 성공으로
  // 끝나면 사용자는 어디에 깔렸는지 알 길이 없다.
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
    throw new Error(`${ROOT_OPTION} must be an existing directory: ${resolved}`);
  }
  // worktree 판정이 realpath 기준이다. 여기서 정규화하지 않으면 심볼릭 링크
  // `--root`가 판정과 다른 경로로 내려가 guard를 비껴간다.
  return fs.realpathSync.native(resolved);
}

// 자식 install에는 이미 해석된 경로를 cwd로 넘긴다. 상대 `--root`를 그대로
// 전달하면 자식이 제 cwd 기준으로 한 번 더 풀어 다른 곳을 가리킨다.
export function withoutInstallRootOption(args) {
  return extractCliOption(args, ROOT_OPTION).args;
}

// `--root`는 사용자가 이름을 댄 자리다. `resolveInstallRoot`가 git root로 걸어
// 올라가 다른 곳에 설치하면 지금 고치는 버그와 같은 모양이 된다 — 요청한 곳이
// 아닌 데 깔리고, 성공 배너는 root를 알리지 않는다.
export function assertInstallRootIsFinal(requested, resolved) {
  if (path.resolve(requested) === path.resolve(resolved)) {
    return;
  }
  throw new Error(`${ROOT_OPTION} ${requested} resolves to ${resolved}; pass --root ${resolved}`);
}

// 갱신 알림. `agent-flow-install.mjs`가 자식 kit install의 stdout에서 이 접두사로
// 시작하는 줄만 되살린다. prune 알림과 같은 통로다.
export const SKILL_UPGRADE_NOTICE_PREFIX = "  ~ upgraded skill: ";

// 사용자 편집이라 건너뛴 것도 알린다. kit.mjs는 여태 무음이었고, 그 무음이
// "kit 개정이 왜 안 왔는가"를 물을 자리 자체를 없앴다.
export const SKILL_SKIP_NOTICE_PREFIX = "  ! skipped (user-modified): ";

// 같은 파일을 복사 단계와 동기화 단계가 각각 판정한다. 둘 다 알리면 사용자는 한 번
// 손댄 파일을 두 번 경고받고, 그 잡음이 정작 갱신된 줄을 덮는다.
const reportedUserEdits = new Set();

export function reportSkippedUserEdit(label) {
  const normalized = String(label).replaceAll("\\", "/");
  if (reportedUserEdits.has(normalized)) {
    return;
  }
  reportedUserEdits.add(normalized);
  console.log(`${SKILL_SKIP_NOTICE_PREFIX}${normalized}`);
}

// skill 본문 밖의 kit 자산은 이름으로 부르는 대상이 아니라 경로로 부른다.
export const ASSET_UPGRADE_NOTICE_PREFIX = "  ~ upgraded: ";

// 사본을 남긴 사실도 알린다. `.agent-flow/`는 gitignore라 `git status`에도 안 뜨고,
// 알리지 않으면 복구 사본에 도달할 경로가 사람에게 하나도 없다.
export const ASSET_BACKUP_NOTICE_PREFIX = "  ~ backup: ";

// `<skill>/SKILL.md`는 index hash가 오라클이다. 두 기록이 같은 파일을 노리면
// 어느 쪽이 이기는지 알 수 없으므로 자산 동기화는 그 파일만 비켜 간다.
export function isBundledSkillManifest(relative) {
  const parts = relative.replaceAll("\\", "/").split("/");
  return parts.length === 2 && parts[1] === "SKILL.md";
}

// `SKILL.md` 밖의 kit 자산(형제 파일, review 템플릿)에는 index 같은 기록이 없어
// 갱신 여부를 판정할 근거가 없었다. 그래서 이 파일에 우리가 쓴 내용의 hash를
// 남긴다 — 다음 install이 "설치본이 아직 우리가 쓴 그대로인가"를 물을 수 있다.
export const KIT_ASSETS_RELATIVE = path.join(".agent-flow", "kit-assets.json");

// 파일 부재와 파싱 실패를 가른다. 잘린 기록을 "기록 없음"으로 읽으면 부트스트랩
// 분기로 떨어져 살아 있는 사용자 편집을 한꺼번에 덮는다. 읽을 수 없으면 null을
// 내고 호출부가 동기화를 통째로 건너뛴다 — 아무것도 쓰지 않는 쪽이 안전하다.
export function assertManagedTreeSafe(root) {
  const managedRoot = path.join(root, ".agent-flow");
  const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
  const visit = (target) => {
    let identity;
    try {
      identity = fs.lstatSync(target);
    } catch (error) {
      if (error?.code === "ENOENT") {
        return;
      }
      throw error;
    }
    const relative = path.relative(root, target);
    if (identity.isSymbolicLink()) {
      throw new Error(`refusing symlinked project managed path: ${relative}`);
    }
    if (currentUid !== null && identity.uid !== currentUid) {
      throw new Error(`project managed path is not owned by the current user: ${relative}`);
    }
    if (identity.isDirectory()) {
      if (process.platform !== "win32" && (identity.mode & 0o022) !== 0) {
        throw new Error(`project managed path has unsafe mode: ${relative}`);
      }
      for (const name of fs.readdirSync(target)) {
        visit(path.join(target, name));
      }
      return;
    }
    if (!identity.isFile()) {
      throw new Error(`project managed path is not a regular file: ${relative}`);
    }
    if (identity.nlink !== 1) {
      throw new Error(`project managed path has unsafe link count: ${relative}`);
    }
  };
  visit(managedRoot);
}

export function pathHasSymlink(root, target) {
  const relative = path.relative(root, target);
  const parts = relative.split(path.sep).filter(Boolean);
  let cursor = root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    let identity;
    try {
      identity = fs.lstatSync(cursor);
    } catch (error) {
      if (error?.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    if (identity.isSymbolicLink()) {
      return true;
    }
  }
  return false;
}

export function assertProjectHookPathsSafe(root) {
  assertManagedTreeSafe(root);
  for (const relative of [
    [".agent-flow"],
    [".agent-flow", "bin"],
    [".agent-flow", "skills"],
    [".agent-flow", "workflows"],
    [".agent-flow", "profiles"],
    [".agent-flow", "templates"],
    [".agent-flow", "scripts"],
    [".agent-flow", "scripts", "hooks"],
    [".agent-flow", "scripts", "hook-runtime"],
    [".agent-flow", "prompts"],
    [".agent-flow", "rules"],
    [".agent-flow", "bootstrap"],
    [".agent-flow", "runtime"],
    [".agent-flow", "memory"],
    [".agent-flow", "runs"],
    [".agent-flow", "state"],
    [".agent-flow", "handoffs"],
    [".agent-flow", "team"],
    [".agent-flow", "worktrees"],
    [".agent-flow", "backups"],
    [".agent-flow", "local-skills"],
  ]) {
    const target = path.join(root, ...relative);
    let identity;
    try {
      identity = fs.lstatSync(target);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
    if (pathHasSymlink(root, target)) {
      throw new Error(`refusing symlinked project managed path: ${target}`);
    }
    if (identity && !identity.isDirectory()) {
      throw new Error(`project managed path is not a directory: ${target}`);
    }
    if (
      identity
      && process.platform !== "win32"
      && (identity.mode & 0o022) !== 0
    ) {
      throw new Error(`project managed path has unsafe mode: ${target}`);
    }
  }
  for (const relative of [
    [".agent-flow", "kit.json"],
    [".agent-flow", "kit-assets.json"],
  ]) {
    const target = path.join(root, ...relative);
    if (pathHasSymlink(root, target)) {
      throw new Error(`refusing symlinked project managed file: ${target}`);
    }
  }
  for (const relative of [
    [".claude", "settings.json"],
    [".Codex", "hooks.json"],
    [".codex", "hooks.json"],
  ]) {
    const target = path.join(root, ...relative);
    if (pathHasSymlink(root, path.dirname(target))) {
      throw new Error(`refusing symlinked project hook registration: ${target}`);
    }
  }
}


export function readKitAssetRecord(root) {
  const target = path.join(root, KIT_ASSETS_RELATIVE);
  if (!fs.existsSync(target)) {
    return new Map();
  }
  try {
    const files = JSON.parse(fs.readFileSync(target, "utf8"))?.files;
    return files && typeof files === "object" ? new Map(Object.entries(files)) : null;
  } catch {
    return null;
  }
}

export function writeKitAssetRecord(root, written) {
  const files = Object.fromEntries([...written.entries()].sort(([left], [right]) => left.localeCompare(right)));
  const target = path.join(root, KIT_ASSETS_RELATIVE);
  writeAtomicTextFile(target, `${JSON.stringify({ version: 1, files }, null, 2)}\n`);
}

// kit 자산 트리 하나를 설치본과 맞춘다. 기록이 아직 없는 자산은(이 구조가 생기기
// 전에 깔린 프로젝트) 사본을 남기고 한 번 갱신한다.
export function syncKitAssets(root, src, dest, recorded, written, { skip = () => false } = {}) {
  for (const source of kitAssetFiles(src)) {
    const relative = path.relative(src, source);
    if (skip(relative)) {
      continue;
    }
    const target = path.join(dest, relative);
    const label = path.relative(root, target).replaceAll("\\", "/");
    const content = fs.readFileSync(source, "utf8");
    if (!fs.existsSync(target)) {
      continue;
    }
    const current = fs.readFileSync(target, "utf8");
    if (current === content) {
      written.set(label, contentHash(content));
      continue;
    }
    const previous = recorded.get(label);
    if (previous !== undefined && previous !== contentHash(current)) {
      reportSkippedUserEdit(label);
      written.set(label, previous);
      continue;
    }
    if (previous === undefined) {
      writeKitAssetBackup(root, target, current);
    }
    fs.writeFileSync(target, content, "utf8");
    written.set(label, contentHash(content));
    console.log(`${ASSET_UPGRADE_NOTICE_PREFIX}${label}`);
  }
}

// 사본은 미러 트리 밖에 쓴다. skill 디렉터리 안에 남기면 `dirContentsMatch`의 항목
// 수 비교가 어긋나 profile을 좁혀도 그 skill이 다시는 지워지지 않는다.
function writeKitAssetBackup(root, target, content) {
  const agentFlowDir = path.join(root, ".agent-flow");
  const backup = path.join(agentFlowDir, "backups", path.relative(agentFlowDir, target));
  fs.mkdirSync(path.dirname(backup), { recursive: true });
  if (fs.existsSync(backup) && fs.readFileSync(backup, "utf8") === content) {
    return;
  }
  const free = fs.existsSync(backup) ? nextFreeBackupPath(backup, content) : backup;
  if (free) {
    fs.writeFileSync(free, content, "utf8");
    console.log(`${ASSET_BACKUP_NOTICE_PREFIX}${path.relative(root, free).replaceAll("\\", "/")}`);
  }
}

function kitAssetFiles(dir) {
  if (!fs.existsSync(dir)) {
    return [];
  }
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const child = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...kitAssetFiles(child));
    } else if (entry.isFile()) {
      found.push(child);
    }
  }
  return found;
}

function contentHash(text) {
  return crypto.createHash("sha256").update(text).digest("hex");
}

// kit이 배포하는 skill은 갱신한다. "내용이 다르면 사용자 편집"으로만 보호하면 새 kit이
// 고친 skill이 기존 설치본에 영영 닿지 않는다 — 실측으로 `workflowPhases`를 더한 개정이
// 설치된 프로젝트 13곳 어디에도 도달하지 못했고, 그래서 그 skill은 계속 활성화되지
// 않았다.
//
// 사용자가 손댔는지는 이전 install이 남긴 hash로 가른다. 그 hash는 설치본
// `SKILL.md` 하나를 가리키므로 덮는 것도 그 파일 하나다. 형제 파일(`references/*`)
// 까지 덮으면 오라클이 없는 파일을 되돌리게 되고, 백업을 skill 디렉터리에 남기면
// `dirContentsMatch`의 항목 수 비교가 어긋나 profile을 좁혀도 그 skill이 지워지지
// 않는다. hash가 맞다는 것은 우리가 쓴 그대로라는 뜻이라 사본도 필요 없다.
export function upgradeBundledSkills(root, src, dest, previousIndex, allowedNames, excludedNames) {
  const recorded = Array.isArray(previousIndex?.skills) ? previousIndex.skills : [];
  for (const skill of recorded) {
    if (!skill?.name || !skill?.hash || !skill?.path) {
      continue;
    }
    // 그림자(project/local 사본)가 있으면 index의 hash는 그 파일 것이다. bundled
    // 사본과 견주면 오라클이 어긋난다 — 그때 bundled 사본은 쓰이지도 않는다.
    if (skill.source !== "bundled" || excludedNames.has(skill.name)) {
      continue;
    }
    if (allowedNames && !allowedNames.has(skill.name)) {
      continue;
    }
    // 디렉터리명은 기록된 경로에서 가져온다. `skill.name`은 frontmatter가 정하므로
    // 디렉터리명과 갈릴 수 있고, 그러면 kit에서 없는 이름을 찾아 영영 수렴하지 않는다.
    const directory = path.basename(path.dirname(skill.path.replaceAll("\\", "/")));
    const shipped = path.join(src, directory, "SKILL.md");
    const installed = path.resolve(root, skill.path);
    ensureChildPath(dest, installed);
    if (!fs.existsSync(shipped) || !fs.existsSync(installed)) {
      continue;
    }
    if (skillFileHash(installed) !== skill.hash) {
      // 갱신을 건너뛴 사실도 알린다. 무음이면 "왜 kit 개정이 안 왔는가"를 물을
      // 자리가 없다 — 그 무음이 원래 버그를 수개월 가렸다.
      if (fs.readFileSync(installed, "utf8") !== fs.readFileSync(shipped, "utf8")) {
        reportSkippedUserEdit(skill.path);
      }
      continue;
    }
    const content = fs.readFileSync(shipped, "utf8");
    if (fs.readFileSync(installed, "utf8") === content) {
      continue;
    }
    fs.writeFileSync(installed, content, "utf8");
    console.log(`${SKILL_UPGRADE_NOTICE_PREFIX}${skill.name}`);
  }
}

// 사용자 편집을 오라클로 승격하지 않는다. index의 hash는 발견 시점의 파일에서
// 뽑으므로, 편집된 bundled skill을 한 번 건너뛰고 나면 다음 install에서는 그 편집
// 내용이 "우리가 쓴 것"으로 기록돼 hash가 일치하고, 그때 편집이 덮인다 — 재설치
// 두 번이면 잃는다. kit 판본과 다른 bundled skill은 이전 기록을 그대로 들고 간다.
export function preserveKitSkillHashes(index, previousIndex, kitSkillsDir) {
  const previous = new Map(
    (Array.isArray(previousIndex?.skills) ? previousIndex.skills : [])
      .filter((skill) => skill?.name && skill?.hash)
      .map((skill) => [skill.name, skill.hash]),
  );
  for (const skill of Array.isArray(index?.skills) ? index.skills : []) {
    if (skill?.source !== "bundled" || !skill?.name || !skill?.path || !skill?.hash) {
      continue;
    }
    const directory = path.basename(path.dirname(String(skill.path).replaceAll("\\", "/")));
    const shipped = path.join(kitSkillsDir, directory, "SKILL.md");
    if (!fs.existsSync(shipped) || skill.hash === skillFileHash(shipped)) {
      continue;
    }
    const carried = previous.get(skill.name);
    if (carried) {
      skill.hash = carried;
    }
  }
  return index;
}

function skillFileHash(pathName) {
  return crypto.createHash("sha256").update(fs.readFileSync(pathName, "utf8")).digest("hex");
}

// git 기반 checkout 판정. 두 install 진입점이 같은 본문을 한 벌씩 들고 있었고,
// 그 사본이 실제로 갈라졌다 — kit은 realpath로, install.mjs는 `path.resolve`로
// worktree를 판정해서 심볼릭 링크 `--root` 하나가 한쪽 guard만 통과했다(#155).
// Python `LEAKY_GIT_ENV_VARS`(`src/agent_flow/core/worktree_isolation.py`)와 같은
// 목록이어야 한다. ambient discovery 변수가 하나라도 남으면 우리 git이 요청한 cwd
// 밖을 본다 - 실측으로 `GIT_COMMON_DIR=/private/tmp/af-decoy/.git`만 있어도
// `rev-parse --git-common-dir`이 decoy를 반환하고, 그 부모가 install PROJECT가 됐다.
const LEAKY_GIT_ENV_VARS = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
  "GIT_CEILING_DIRECTORIES",
];

const WORKTREE_MARKERS = new Set([".agent-flow", ".codex", ".Codex", ".omp"]);

export function canonicalPath(value) {
  try {
    return fs.realpathSync.native(value);
  } catch {
    return path.resolve(value);
  }
}

export function samePath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    // 심볼릭 링크가 섞인 임시 경로에서도 홈 비교는 보수적으로 처리한다.
    return path.resolve(left) === path.resolve(right);
  }
}

export function gitEnv() {
  const env = { ...process.env };
  for (const name of LEAKY_GIT_ENV_VARS) {
    delete env[name];
  }
  // 분기 대상 메시지를 결정적으로 유지한다. 이 명령에만 적용된다.
  env.LC_ALL = "C";
  env.LANG = "C";
  return env;
}

const GIT_RELAY_TIMEOUT_MS = 30_000;

export function gitOutput(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    env: gitEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    // 멈춘 git 하나가 CLI를 무한 대기시킨다. worktree 판정이 install 최상단에서
    // 돌기 때문에 상한이 없으면 그 지점에서 통째로 멈춘다.
    timeout: GIT_RELAY_TIMEOUT_MS,
    killSignal: "SIGKILL",
  });
  if (result.error) {
    // git이 없는 환경은 정상 경로다(non-git 프로젝트). 그 외 spawn 실패는
    // "돌긴 했는데 대답을 못 받은" 경우다 - 조용히 null로 접으면 어느 저장소인지
    // 모르는 채로 경로 스캔 폴백까지 내려간다.
    if (result.error.code === "ENOENT") {
      return null;
    }
    throw new Error(`git ${args.join(" ")} failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    return null;
  }
  const output = result.stdout.trim();
  return output || null;
}

export function resolveManagedWorktreeContext(start) {
  const resolved = canonicalPath(start);
  const configuredStateHome = process.env.XDG_STATE_HOME;
  const stateHome = configuredStateHome === "~"
    ? HOME
    : /^~[\\/]/.test(configuredStateHome || "") && HOME
      ? path.join(HOME, configuredStateHome.slice(2))
      : configuredStateHome || (HOME ? path.join(HOME, ".agent-flow") : "");
  if (stateHome) {
    const centralRoot = canonicalPath(path.join(stateHome, "worktrees"));
    if (samePath(resolved, centralRoot) || resolved.startsWith(`${centralRoot}${path.sep}`)) {
      return null;
    }
  }
  const parts = resolved.split(path.sep);
  // `<marker>/worktrees`로 **끝나는** 경로도 관리 경로다. 여기를 놓치면
  // `<X>/.codex/worktrees` 안에 설치본이 생긴다 - 형제 checkout을 담는 컨테이너다.
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!WORKTREE_MARKERS.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    if (HOME && samePath(root, HOME) && parts[index] !== ".agent-flow") {
      continue;
    }
    return { root, name: parts[index + 2] ?? null };
  }
  return null;
}

export function resolveManagedWorktreeRoot(start) {
  return resolveManagedWorktreeContext(start)?.root ?? null;
}

export function resolveGitCommonWorktreeRoot(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(start, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  // `git rev-parse --git-common-dir`은 **cwd 기준** 상대경로("../.git" 등)를 낸다.
  // topLevel 기준으로 풀면 cwd가 하위 디렉토리일 때 한 단계씩 어긋난 경로가 나온다.
  // git은 심볼릭 링크를 푼 실경로 기준으로 세므로 여기도 canonical로 맞춘다 -
  // 아니면 링크 경로의 깊이만큼 어긋난 디렉터리가 leader로 나온다.
  const resolvedCommonDir = path.resolve(canonicalPath(start), commonDir);
  if (path.basename(resolvedCommonDir) !== ".git") {
    return null;
  }
  return path.dirname(resolvedCommonDir);
}

// linked worktree 판정. leader를 cwd와 직접 비교하면 `<leader>/src`처럼 leader의
// 하위 디렉토리에서 install하는 정상 경로까지 막힌다 - 그래서 이 checkout의
// toplevel과 견준다. linked worktree에서만 toplevel(worktree root)과
// leader(git common dir의 부모)가 갈라진다.
export function resolveLinkedWorktreeLeader(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const leader = resolveGitCommonWorktreeRoot(start);
  if (!topLevel || !leader || samePath(leader, topLevel)) {
    return null;
  }
  return leader;
}

export function resolveInstallRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot) {
    return gitCommonRoot;
  }
  const parts = start.split(path.sep);
  const markerIndex = parts.lastIndexOf(".agent-flow");
  if (markerIndex !== -1) {
    return parts.slice(0, markerIndex).join(path.sep) || path.sep;
  }
  // non-git 프로젝트(git repo가 아니거나 git 실행 불가)의 마지막 수단이다. 위의 git
  // 기반 판정이 전부 null을 낸 경우이므로, 여기서 start를 그대로 install root로 쓴다.
  return start;
}

// `.agent-flow/profiles/`는 "이 프로젝트에 걸리는 profile"을 사람에게 보여 주는
// 자리다. kit이 배포하는 전부를 깔면 남의 stack 정의가 9개 쌓여서 그 구분이
// 사라진다 — 실제로 android 프로젝트에서 nextjs.yaml을 고치는 오류가 났다.
//
// `generic`은 빼지 않는다. runner가 profile을 못 찾을 때 마지막으로 읽는 fallback
// 이라(`runner.py` `_load_single_profile`) 이 프로젝트에 항상 걸린다.
// `_schema.yaml`은 나머지 파일의 필드 정의서다.
export const ALWAYS_INSTALLED_PROFILE_FILES = new Set(["_schema.yaml", "generic.yaml"]);

// escalation은 없다. profile은 자기 어휘만 선언하고 다른 profile의 표를 지명하지
// 않는다(`skills.required_review[*].profiles`는 표와 함께 사라졌다) — react-native의
// `android/**` 변경은 자기 `skills.external` domain이 경로로 좁혀서 잡는다. 그래서
// 걸리는 profile은 여기 들어온 id 전부이고, 그 이상 따라갈 곳이 없다.
export function installedProfileFileNames(profileIds) {
  const names = new Set(ALWAYS_INSTALLED_PROFILE_FILES);
  for (const profileId of profileIds) {
    if (typeof profileId === "string" && profileId) {
      names.add(`${profileId}.yaml`);
    }
  }
  return names;
}

// 감지된 profile은 `--profile` 선택과 무관하게 항상 넣는다. kit.json의 `profile`
// 필드가 그 값을 기록하고, worktree에는 kit.json이 없어서 `active_profile_ids`가
// `detect_profile`로 떨어진다(`cli.py`의 `gates`/`architecture-lint --worktree`).
// 즉 감지 id는 선택하지 않아도 이 프로젝트에 걸린다.
export function activeInstallProfileIds(detectedProfile, installSelection) {
  const selected = Array.isArray(installSelection?.profiles) ? installSelection.profiles : [];
  return [detectedProfile, ...selected].filter((value) => typeof value === "string" && value);
}

// 이전 설치본이 받아 둔 남의 stack profile을 걷어낸다. kit이 배포하는 이름만
// 지운다 — 사용자가 만든 custom profile은 `src`에 없으므로 살아남는다.
//
// 내용이 지금 배포본과 같으면 kit 사본을 그대로 지우는 것이라 잃는 것이 없다.
// 그때 알림을 내면 정리했다는 사실만 매번 되풀이한다. 다르면 사용자가 손댔을 수
// 있으므로 사본을 남기고 알린다.
export function pruneUninstalledProfiles(root, src, dest, keepNames) {
  if (!fs.existsSync(dest)) {
    return 0;
  }
  let pruned = 0;
  for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
    if (!entry.isFile() || keepNames.has(entry.name)) {
      continue;
    }
    const bundledPath = path.join(src, entry.name);
    if (!fs.existsSync(bundledPath)) {
      continue;
    }
    const target = path.join(dest, entry.name);
    if (fs.readFileSync(target, "utf8") !== fs.readFileSync(bundledPath, "utf8")) {
      const backup = writePruneBackup(target);
      console.log(
        `${PRUNE_NOTICE_PREFIX}${path.relative(root, target)}` +
          ` (backup: ${path.relative(root, backup)})`,
      );
    }
    fs.rmSync(target);
    pruned += 1;
  }
  return pruned;
}

export function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

export function backupIfDifferent(root, target, content) {
  if (!fs.existsSync(target)) {
    return null;
  }
  if (fs.readFileSync(target, "utf8") === content) {
    return null;
  }
  // 덮어쓰는 내용을 잃지 않는다. 고정 이름 하나만 쓰면 둘 중 하나를 반드시
  // 버리게 된다 — 매번 덮으면 사용자 원본이, 안 덮으면 이번 편집이 사라진다.
  const backup = nextFreeBackupPath(`${target}.bak`, fs.readFileSync(target, "utf8"));
  if (backup === null) {
    return null;  // 같은 내용이 이미 백업돼 있다.
  }
  fs.copyFileSync(target, backup);
  console.log(`  ~ replaced ${path.relative(root, target)} (backup: ${path.relative(root, backup)})`);
  return backup;
}

export function ensureChildPath(parent, child) {
  const parentResolved = path.resolve(parent);
  const childResolved = path.resolve(child);
  const relative = path.relative(parentResolved, childResolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`path escapes parent: ${child}`);
  }
}

export function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function hasChildWithSuffix(rootDir, suffix) {
  if (!fs.existsSync(rootDir)) {
    return false;
  }
  return fs.readdirSync(rootDir).some((name) => name.endsWith(suffix));
}

export function isGitignoreEntryCovered(entry, existing) {
  if (existing.has(entry)) {
    return true;
  }
  const normalized = entry.replace(/^\/+/, "");
  const parts = normalized.split("/");
  for (let index = 1; index < parts.length; index += 1) {
    const parent = `${parts.slice(0, index).join("/")}/`;
    const parentWithoutSlash = parent.replace(/\/$/, "");
    if (
      existing.has(parent) ||
      existing.has(parentWithoutSlash) ||
      existing.has(`/${parent}`) ||
      existing.has(`/${parentWithoutSlash}`)
    ) {
      return true;
    }
  }
  return false;
}


export function nextFreeBackupPath(base, content) {
  // 이미 같은 내용이 백업돼 있으면 사본을 늘리지 않는다. 다르면 비어 있는
  // 다음 이름을 찾는다. 무엇도 덮지 않으므로 어떤 버전도 잃지 않는다.
  for (let i = 0; i < 100; i += 1) {
    const candidate = i === 0 ? base : `${base}.${i}`;
    if (!fs.existsSync(candidate)) {
      return candidate;
    }
    if (fs.readFileSync(candidate, "utf8") === content) {
      return null;
    }
  }
  return null;
}

export function planReviewerSkillMarkdown() {
  return `---\nname: plan-reviewer\ndescription: Use during the full-feature plan-review phase.\n---\n\n# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function cleanupFaultPoint(name) {
  if (process.env.AGENT_FLOW_TEST_PUBLISH_FAULT === name) {
    throw new Error(`injected cleanup fault: ${name}`);
  }
}


function backupAndRemoveFiles(root, targets, message) {
  const planned = targets
    .filter((target) => fs.existsSync(target) && fs.statSync(target).isFile())
    .map((target) => {
      const content = fs.readFileSync(target, "utf8");
      return {
        target,
        content,
        mode: fs.statSync(target).mode & 0o777,
        backup: nextFreeBackupPath(`${target}.removed`, content),
      };
    });
  const removed = [];
  try {
    for (const item of planned) {
      if (item.backup !== null) {
        fs.copyFileSync(item.target, item.backup);
        fs.chmodSync(item.backup, 0o644);
      }
    }
    cleanupFaultPoint("after-backup");
    for (const item of planned) {
      fs.rmSync(item.target);
      removed.push(item);
    }
  } catch (error) {
    for (const item of removed.reverse()) {
      fs.writeFileSync(
        item.target,
        item.content,
        { encoding: "utf8", mode: item.mode },
      );
      fs.chmodSync(item.target, item.mode);
    }
    throw error;
  }
  for (const item of planned) {
    console.log(message(item.target));
  }
}


export function pruneRetiredManagedScripts(root) {
  const scriptsDir = path.join(root, ".agent-flow", "scripts");
  const targets = ["check-context-docs.mjs", "check-context-docs.ts"]
    .map((scriptName) => path.join(scriptsDir, scriptName));
  backupAndRemoveFiles(
    root,
    targets,
    (target) => `  - removed retired script: ${path.relative(root, target)}`,
  );
}

export function readHookSettings(settingsPath) {
  if (!fs.existsSync(settingsPath)) {
    return {};
  }
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch {
    const backupPath = `${settingsPath}.bak`;
    fs.copyFileSync(settingsPath, backupPath);
    console.error(`warning: could not parse ${settingsPath}; backed up to ${backupPath} before overwriting`);
    return {};
  }
}

export function removeGitignoreEntries(pathName, entries) {
  if (!fs.existsSync(pathName)) return;
  const removals = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const lines = current.split(/\r?\n/);
  const filtered = lines.filter((line) => !removals.has(line.trim()));
  if (filtered.length === lines.length) return;
  const next = `${filtered.join("\n").replace(/\n*$/, "")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

export function removeLegacyProjectSkillCopies(projectRoot, skillName) {
  for (const parent of [
    path.join(projectRoot, ".agent-flow", "skills"),
    path.join(projectRoot, ".claude", "skills"),
    path.join(projectRoot, ".codex", "skills"),
    path.join(projectRoot, ".Codex", "skills"),
    path.join(projectRoot, ".omp", "skills"),
    path.join(projectRoot, ".gemini", "skills"),
    path.join(projectRoot, ".gemini", "antigravity", "skills"),
  ]) {
    fs.rmSync(path.join(parent, skillName), { recursive: true, force: true });
  }
}

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

export function tomlBasicString(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
}

export function uniqueStrings(values) {
  return [...new Set(values.map(String).filter(Boolean))];
}

export function unquoteShellWord(value) {
  if (typeof value !== "string") {
    return "";
  }
  if (value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).replaceAll("'\\''", "'");
  }
  return value;
}

export function upsertGitignore(pathName, entries) {
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const lines = current.split(/\r?\n/);
  const existing = new Set(lines.map((line) => line.trim()));
  const missing = entries.filter((entry) => !isGitignoreEntryCovered(entry, existing));
  if (missing.length === 0) {
    return;
  }
  const prefix = current.trimEnd();
  const next = `${prefix}${prefix ? "\n" : ""}${missing.join("\n")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

export function validateSkillDependencies(skills) {
  const names = new Set(skills.map((skill) => skill.name));
  const warnings = [];
  for (const skill of skills) {
    for (const required of skill.requires || []) {
      if (!names.has(required)) {
        warnings.push(`${skill.name}: missing required skill ${required}`);
      }
    }
  }
  return warnings;
}

export const AGENT_FLOW_COMMAND = "agent-flow";

export const HOME = process.env.HOME || process.env.USERPROFILE || "";

export const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export const PRUNE_BACKUP_SUFFIX = ".removed";

export const PRUNE_BACKUP_VERSIONED = /\.removed\.[0-9a-f]{8}$/;

export const SKILL_INDEX_START = "<!-- agent-flow:skills:start -->";

export const SKILL_INDEX_END = "<!-- agent-flow:skills:end -->";

// Read 계열 tool 이름은 host마다 다르다. comment-checker의 write matcher와 같은 방식으로 합집합을 쓴다.
export const READ_TOOL_MATCHER = "^(Read|read|read_file|view|cat)$";

// Skill tool 사용은 SKILL.md Read를 발생시키지 않는다(Claude Code 문서: "does not re-read
// the skill file on later turns"). Codex에는 Read tool이 없어 셸로 파일을 연다. 관측
// matcher가 Read만 보면 두 host에서 사용 증거가 항상 비어 있다.
export const SKILL_USE_TOOL_MATCHER =
  "^(Read|read|read_file|view|cat|Skill|skill|Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

// 셸 실행 tool도 host마다 이름이 다르다. 관측 전용이라 PostToolUse에만 붙는다.
export const COMMAND_TOOL_MATCHER = "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

const HOST_WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|write_file|edit_file|Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

export function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Run two independent active-host reviewer sub-agents before approve. Each reviewer section must include \`reviewer-source: sub-agent\`; optional cross-host reviewers are extra evidence and do not replace active-host reviewers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\ncodex-claude-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
}

export function fullFeatureSkillMarkdown() {
  return `---\nname: full-feature-workflow\ndescription: Use this skill for feature work in this project.\n---\n\n# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${AGENT_FLOW_COMMAND} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.\n`;
}

export function productBriefSkillMarkdown() {
  return `---\nname: product-brief\ndescription: Use during the full-feature product-brief phase.\n---\n\n# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

export function pushWatchSkillMarkdown() {
  return `---\nname: push-watch\ndescription: Use this skill after local verification is complete and the branch is ready to publish.\n---\n\n# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked: main, master, develop.\n- Record PR watch state with \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n- merge requires explicit approval. Do not merge unattended.\n`;
}

const LEGACY_PROJECT_LAUNCHER_RELATIVE = path.join(".agent-flow", "bin", "agent-flow");
const LEGACY_PROJECT_RUNTIME_RELATIVE = path.join(".agent-flow", "runtime", "python");

// 전역 immutable bundle의 dependency source는 설치 대상 project와 분리된 interpreter만
// 쓴다. 상대 PATH나 project 안의 executable을 먼저 실행한 뒤 검사하면 이미 project
// code가 installer 권한으로 실행된 뒤이므로 spawn 전에 경로와 소유 경계를 검증한다.
const managedPythonCache = new Map();

function pathIsWithin(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function trustedPythonExecutable(candidate, projectRoot) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
    return null;
  }
  const normalized = path.resolve(candidate);
  if (pathIsWithin(projectRoot, normalized)) {
    return null;
  }
  let resolved;
  try {
    resolved = fs.realpathSync.native(normalized);
    if (pathIsWithin(fs.realpathSync.native(projectRoot), resolved)) {
      return null;
    }
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    const trustedIdentity = (identity) => (
      currentUid === null
      || (
        (identity.uid === 0 || identity.uid === currentUid)
        && (identity.mode & 0o022) === 0
      )
    );
    const executable = fs.statSync(normalized);
    if (
      !executable.isFile()
      || !trustedIdentity(executable)
      || (process.platform !== "win32" && (executable.mode & 0o111) === 0)
    ) {
      return null;
    }
    if (process.platform !== "win32") {
      const trustedDirectoryChain = (start) => {
        let cursor = start;
        while (true) {
          const identity = fs.statSync(cursor);
          if (!identity.isDirectory() || !trustedIdentity(identity)) {
            return false;
          }
          const parent = path.dirname(cursor);
          if (parent === cursor) {
            return true;
          }
          cursor = parent;
        }
      };
      if (
        !trustedDirectoryChain(path.dirname(normalized))
        || !trustedDirectoryChain(path.dirname(resolved))
      ) {
        return null;
      }
    }
  } catch {
    return null;
  }
  // venv는 symlink 경로에서 `pyvenv.cfg`를 찾으므로 검증한 candidate 표기를 유지한다.
  return { python: normalized, realpath: resolved };
}

function managedPythonCandidates() {
  const names = [
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
  ];
  const values = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
  ];
  for (const entry of (process.env.PATH || "").split(path.delimiter)) {
    // 빈 PATH 요소와 "."를 포함한 상대 요소는 current directory를 검색한다.
    if (!path.isAbsolute(entry)) {
      continue;
    }
    for (const name of names) {
      values.push(path.join(entry, name));
      if (process.platform === "win32") {
        values.push(path.join(entry, `${name}.exe`));
      }
    }
  }
  values.push("/usr/bin/python3", "/usr/local/bin/python3");
  return [...new Set(values.filter(Boolean))];
}

export function resolveManagedPython({ projectRoot, kitRoot }) {
  if (!path.isAbsolute(projectRoot) || !path.isAbsolute(kitRoot)) {
    throw new TypeError("projectRoot and kitRoot must be absolute paths");
  }
  const cacheKey = `${canonicalPath(projectRoot)}\0${canonicalPath(kitRoot)}`;
  const cached = managedPythonCache.get(cacheKey);
  if (cached) {
    return cached;
  }
  const candidates = managedPythonCandidates();
  const trustedCandidates = [];
  for (const candidate of candidates) {
    const executable = trustedPythonExecutable(candidate, projectRoot);
    if (
      executable !== null
      && !trustedCandidates.some((trusted) => trusted.python === executable.python)
    ) {
      trustedCandidates.push(executable);
    }
  }
  for (const executable of trustedCandidates) {
    const probe = spawnSync(
      executable.python,
      ["-I", "-c", "import click, yaml"],
      { encoding: "utf8", timeout: 15_000 },
    );
    if (!probe.error && probe.status === 0) {
      const managedPython = { ...executable, flag: "-I" };
      managedPythonCache.set(cacheKey, managedPython);
      return managedPython;
    }
  }
  throw new Error(
    "no trusted Python with Click and PyYAML found for the managed runtime. "
    + `Tried: ${trustedCandidates.map(({ python }) => python).join(", ") || "(none)"}`,
  );
}

function sameFileIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function readOwnedRetirementFile(target, expectedMode = null) {
  let descriptor;
  try {
    descriptor = fs.openSync(
      target,
      fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0),
    );
    const identity = fs.fstatSync(descriptor);
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    if (
      !identity.isFile()
      || identity.nlink !== 1
      || (currentUid !== null && identity.uid !== currentUid)
      || (expectedMode !== null && (identity.mode & 0o777) !== expectedMode)
    ) {
      return null;
    }
    return fs.readFileSync(descriptor);
  } catch {
    return null;
  } finally {
    if (descriptor !== undefined) {
      fs.closeSync(descriptor);
    }
  }
}

function trustedLegacyLauncherDigest(root, previousKit) {
  const recordedDigest = previousKit?.project_launcher_digest;
  if (typeof recordedDigest !== "string" || !/^[0-9a-f]{64}$/.test(recordedDigest)) {
    return null;
  }
  const kitContent = readOwnedRetirementFile(
    path.join(root, ".agent-flow", "kit.json"),
  );
  const registryContent = readOwnedRetirementFile(
    path.join(agentFlowHome(), "managed-projects.json"),
    0o600,
  );
  if (kitContent === null || registryContent === null) {
    return null;
  }
  try {
    const manifest = JSON.parse(kitContent.toString("utf8"));
    const registry = JSON.parse(registryContent.toString("utf8"));
    const resolvedRoot = fs.realpathSync.native(root);
    const projectRecord = registry?.projects?.[resolvedRoot];
    const accepted = [
      projectRecord?.kit_digest,
      ...(Array.isArray(projectRecord?.accepted_kit_digests)
        ? projectRecord.accepted_kit_digests
        : []),
    ];
    const kitDigest = crypto.createHash("sha256").update(kitContent).digest("hex");
    if (
      manifest?.project_launcher_digest === recordedDigest
      && projectRecord?.root === resolvedRoot
      && accepted.includes(kitDigest)
    ) {
      return recordedDigest;
    }
  } catch {
    return null;
  }
  return null;
}


function ownedLegacyLauncher(target, recordedDigest) {
  if (typeof recordedDigest !== "string" || !/^[0-9a-f]{64}$/.test(recordedDigest)) {
    return null;
  }
  let descriptor;
  try {
    descriptor = fs.openSync(
      target,
      fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0),
    );
    const identity = fs.fstatSync(descriptor);
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    if (
      !identity.isFile()
      || identity.nlink !== 1
      || (currentUid !== null && identity.uid !== currentUid)
    ) {
      return null;
    }
    const actual = crypto.createHash("sha256").update(fs.readFileSync(descriptor)).digest("hex");
    return actual === recordedDigest ? identity : null;
  } catch {
    return null;
  } finally {
    if (descriptor !== undefined) {
      fs.closeSync(descriptor);
    }
  }
}

function nextRetiredBackupPath(root, relative) {
  const base = path.join(
    root,
    ".agent-flow",
    "backups",
    "retired",
    path.relative(".agent-flow", relative),
  );
  fs.mkdirSync(path.dirname(base), { recursive: true });
  for (let index = 0; index < 1000; index += 1) {
    const candidate = index === 0 ? base : `${base}.${index}`;
    if (fs.lstatSync(candidate, { throwIfNoEntry: false }) === undefined) {
      return candidate;
    }
  }
  throw new Error(`no backup path is available for retired project asset: ${relative}`);
}

// 프로젝트 안의 옛 실행 경로는 더 이상 신뢰 경계가 아니다. Publish가 이전
// manifest를 바꾸기 전에 ownership oracle을 고정하고, 성공한 cutover만 이를
// 한 번 소비한다.
export function prepareLegacyProjectRuntimeRetirement(root, previousKit = null) {
  assertManagedTreeSafe(root);
  const resolvedRoot = fs.realpathSync.native(root);
  const trustedLauncherDigest = trustedLegacyLauncherDigest(resolvedRoot, previousKit);
  let pending = true;
  return () => {
    if (!pending) {
      throw new Error("legacy project runtime retirement was already attempted");
    }
    pending = false;
    return retireLegacyProjectRuntime(resolvedRoot, trustedLauncherDigest);
  };
}


function retireLegacyProjectRuntime(root, trustedLauncherDigest) {
  assertManagedTreeSafe(root);
  const entries = [
    {
      relative: LEGACY_PROJECT_LAUNCHER_RELATIVE,
      recordedDigest: trustedLauncherDigest,
    },
    {
      relative: LEGACY_PROJECT_RUNTIME_RELATIVE,
      recordedDigest: null,
    },
  ]
    .map((entry) => {
      const source = path.join(root, entry.relative);
      const identity = fs.lstatSync(source, { throwIfNoEntry: false });
      if (identity === undefined) {
        return null;
      }
      return {
        ...entry,
        source,
        identity,
        backup: nextRetiredBackupPath(root, entry.relative),
        ownedIdentity: entry.recordedDigest
          ? ownedLegacyLauncher(source, entry.recordedDigest)
          : null,
      };
    })
    .filter(Boolean);
  const moved = [];
  try {
    for (const entry of entries) {
      const current = fs.lstatSync(entry.source, { throwIfNoEntry: false });
      if (current === undefined || !sameFileIdentity(current, entry.identity)) {
        throw new Error(`legacy project asset changed during retirement: ${entry.relative}`);
      }
      fs.renameSync(entry.source, entry.backup);
      moved.push(entry);
    }
  } catch (error) {
    const rollbackErrors = [];
    for (const entry of moved.reverse()) {
      try {
        if (
          fs.lstatSync(entry.source, { throwIfNoEntry: false }) === undefined
          && fs.lstatSync(entry.backup, { throwIfNoEntry: false }) !== undefined
        ) {
          fs.renameSync(entry.backup, entry.source);
        }
      } catch (rollbackError) {
        rollbackErrors.push(
          rollbackError instanceof Error ? rollbackError.message : String(rollbackError),
        );
      }
    }
    if (rollbackErrors.length > 0) {
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}; rollback failed: ${rollbackErrors.join("; ")}`,
      );
    }
    throw error;
  }

  let pending = true;
  const assertPending = () => {
    if (!pending) {
      throw new Error("legacy project runtime retirement is already settled");
    }
  };
  return {
    commit() {
      assertPending();
      for (const entry of moved) {
        const current = fs.lstatSync(entry.backup, { throwIfNoEntry: false });
        if (current === undefined || !sameFileIdentity(current, entry.identity)) {
          throw new Error(`retired project asset changed before commit: ${entry.relative}`);
        }
      }
      const preserved = [];
      for (const entry of moved) {
        if (entry.ownedIdentity !== null) {
          fs.unlinkSync(entry.backup);
          continue;
        }
        preserved.push(entry.backup);
        console.log(
          `${ASSET_BACKUP_NOTICE_PREFIX}${path.relative(root, entry.source)} retired to `
          + path.relative(root, entry.backup),
        );
      }
      pending = false;
      return preserved;
    },
    rollback() {
      assertPending();
      for (const entry of moved) {
        if (fs.lstatSync(entry.source, { throwIfNoEntry: false }) !== undefined) {
          throw new Error(`legacy project asset reappeared before rollback: ${entry.relative}`);
        }
        const current = fs.lstatSync(entry.backup, { throwIfNoEntry: false });
        if (current === undefined || !sameFileIdentity(current, entry.identity)) {
          throw new Error(`retired project asset changed before rollback: ${entry.relative}`);
        }
      }
      for (const entry of [...moved].reverse()) {
        fs.renameSync(entry.backup, entry.source);
      }
      pending = false;
    },
  };
}


export function writeAtomicTextFile(target, content, mode = 0o600) {
  try {
    const identity = fs.lstatSync(target);
    if (!identity.isFile() || identity.isSymbolicLink()) {
      throw new Error(`refusing to replace non-regular or symlink target: ${target}`);
    }
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw error;
    }
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const staging = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  let descriptor = null;
  let stagingIdentity = null;
  try {
    descriptor = fs.openSync(staging, "wx", mode);
    stagingIdentity = fs.fstatSync(descriptor);
    if (!stagingIdentity.isFile() || stagingIdentity.nlink !== 1) {
      throw new Error(`temporary publish target is not a private regular file: ${staging}`);
    }
    fs.writeFileSync(descriptor, content, { encoding: "utf8" });
    fs.fchmodSync(descriptor, mode);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    const publishedIdentity = fs.lstatSync(staging);
    if (!sameFileIdentity(stagingIdentity, publishedIdentity)) {
      throw new Error(`temporary publish target identity changed: ${staging}`);
    }
    fs.renameSync(staging, target);
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // 원래 write 오류를 보존한다.
      }
    }
    try {
      fs.rmSync(staging, { force: true });
    } catch {
      // 이 정리 실패보다 먼저 발생한 write 또는 rename 오류를 보존한다.
    }
  }
}

export function syncManagedWorktreeHostHooks(root) {
  const resolvedRoot = fs.realpathSync.native(root);
  const topLevel = gitOutput(resolvedRoot, ["rev-parse", "--show-toplevel"]);
  if (topLevel === null) {
    const gitMarker = fs.lstatSync(path.join(resolvedRoot, ".git"), { throwIfNoEntry: false });
    if (gitMarker !== undefined) {
      throw new Error("cannot resolve repository root before host hook sync");
    }
    return true;
  }
  const worktrees = gitOutput(resolvedRoot, ["worktree", "list", "--porcelain", "-z"]);
  if (worktrees === null) {
    throw new Error("cannot enumerate linked worktrees before host hook sync");
  }
  const linked = worktrees
    .split("\0")
    .filter((line) => line.startsWith("worktree "))
    .map((line) => line.slice("worktree ".length))
    .some((checkout) => !samePath(checkout, topLevel));
  if (!linked) {
    return true;
  }
  const invocation = sharedHookLauncherInvocation({ homeDir: agentFlowHome() });
  const result = spawnSync(
    invocation.python,
    [
      "-I",
      "-c",
      invocation.bootstrap,
      "--root",
      resolvedRoot,
      "--cli",
      "worktree",
      "sync-host-hooks",
      "--root",
      resolvedRoot,
    ],
    {
      cwd: resolvedRoot,
      encoding: "utf8",
      timeout: 30_000,
    },
  );
  if (result.error || result.status !== 0) {
    const detail = (
      result.stderr
      || result.stdout
      || (result.error instanceof Error ? result.error.message : String(result.error))
      || "unknown error"
    ).trim();
    throw new Error(`managed worktree host hook sync failed: ${detail}`);
  }
  if (result.stderr) {
    process.stderr.write(result.stderr);
  }
  return true;
}


export function hookEventCommand(_root, eventName) {
  if (!Object.hasOwn(MANAGED_HOOK_POLICY_SEQUENCES, eventName)) {
    throw new Error(`unsupported managed hook event: ${eventName}`);
  }
  const invocation = sharedHookLauncherInvocation({
    homeDir: agentFlowHome(),
    kitRoot: KIT_ROOT,
  });
  return (
    `${shellQuote(invocation.python)} -I -c ${shellQuote(invocation.bootstrap)} `
    + `--event ${shellQuote(eventName)}`
  );
}

export function isPruneBackupName(name) {
  return name.endsWith(PRUNE_BACKUP_SUFFIX) || PRUNE_BACKUP_VERSIONED.test(name);
}

export function writePruneBackup(target) {
  const content = fs.readFileSync(target);
  const primary = `${target}${PRUNE_BACKUP_SUFFIX}`;
  if (!fs.existsSync(primary)) {
    fs.writeFileSync(primary, content);
    return primary;
  }
  if (fs.readFileSync(primary).equals(content)) {
    return primary;
  }
  const digest = crypto.createHash("sha256").update(content).digest("hex").slice(0, 8);
  const versioned = `${primary}.${digest}`;
  if (!fs.existsSync(versioned) || !fs.readFileSync(versioned).equals(content)) {
    fs.writeFileSync(versioned, content);
  }
  return versioned;
}

function literalShellArgv(command) {
  if (typeof command !== "string" || !command.trim()) {
    return null;
  }
  const argv = [];
  let word = "";
  let started = false;
  let quote = null;
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (quote === "'") {
      if (char === "'") {
        quote = null;
      } else {
        word += char;
      }
      continue;
    }
    if (quote === '"') {
      if (char === '"') {
        quote = null;
      } else if (char === "\\" && index + 1 < command.length) {
        const escaped = command[index + 1];
        if (!['"', "\\"].includes(escaped)) {
          return null;
        }
        word += escaped;
        index += 1;
      } else if (char === "$" || char === "`" || char === "\n" || char === "\r") {
        return null;
      } else {
        word += char;
      }
      continue;
    }
    if (/\s/.test(char)) {
      if (started) {
        argv.push(word);
        word = "";
        started = false;
      }
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      started = true;
      continue;
    }
    if (char === "\\") {
      if (index + 1 >= command.length || command[index + 1] === "\n") {
        return null;
      }
      word += command[index + 1];
      started = true;
      index += 1;
      continue;
    }
    if (";&|<>`$()".includes(char)) {
      return null;
    }
    word += char;
    started = true;
  }
  if (quote !== null) {
    return null;
  }
  if (started) {
    argv.push(word);
  }
  return argv;
}

function directHookScriptName(argv, root, scriptNames) {
  if (!argv || typeof root !== "string" || !path.isAbsolute(root)) {
    return null;
  }
  let candidate = null;
  if (argv.length === 1) {
    candidate = argv[0];
  } else if (argv.length === 2 && argv[0] === "/bin/bash" && argv[1].endsWith(".sh")) {
    candidate = argv[1];
  } else if (
    argv.length === 3
    && argv[0] === "/usr/bin/python3"
    && argv[1] === "-I"
    && argv[2].endsWith(".py")
  ) {
    candidate = argv[2];
  }
  if (candidate === null) {
    return null;
  }
  const commandPath = candidate.replaceAll("\\", "/");
  const normalizedRoot = path.resolve(root).replaceAll("\\", "/");
  for (const scriptName of scriptNames) {
    if (
      commandPath === `.agent-flow/scripts/hooks/${scriptName}`
      || commandPath === `scripts/hooks/${scriptName}`
      || commandPath === `${normalizedRoot}/.agent-flow/scripts/hooks/${scriptName}`
      || commandPath === `${normalizedRoot}/scripts/hooks/${scriptName}`
    ) {
      return scriptName;
    }
  }
  return null;
}

function exactLegacyCdHookScriptName(command, root, scriptNames) {
  if (typeof command !== "string" || typeof root !== "string" || !path.isAbsolute(root)) {
    return null;
  }
  const normalizedRoot = path.resolve(root);
  for (const scriptName of scriptNames) {
    const expected = (
      `cd ${shellQuote(normalizedRoot)} && `
      + shellQuote(path.join(normalizedRoot, ".agent-flow", "scripts", "hooks", scriptName))
    );
    if (command === expected) {
      return scriptName;
    }
  }
  return null;
}


export function managedHookScriptName(command, root) {
  const argv = literalShellArgv(command);
  const legacy = directHookScriptName(argv, root, MANAGED_HOOK_SCRIPTS);
  if (legacy) {
    return legacy;
  }
  const legacyCd = exactLegacyCdHookScriptName(command, root, MANAGED_HOOK_SCRIPTS);
  if (legacyCd) {
    return legacyCd;
  }
  const invocation = sharedHookLauncherInvocation({
    homeDir: agentFlowHome(),
    kitRoot: KIT_ROOT,
  });
  // 전환 중인 이전 bootstrap도 state가 승인한 digest면 managed registration이다.
  const current = (
    argv
    && path.isAbsolute(argv[0])
    && argv[1] === "-I"
    && argv[2] === "-c"
    && typeof argv[3] === "string"
    && invocation.bootstrapDigests.has(
      crypto.createHash("sha256").update(argv[3]).digest("hex"),
    )
  );
  if (
    current
    && argv.length === 6
    && argv[4] === "--event"
    && Object.hasOwn(MANAGED_HOOK_POLICY_SEQUENCES, argv[5])
  ) {
    return `@${argv[5]}`;
  }
  if (
    current
    && argv.length === 8
    && typeof root === "string"
    && path.isAbsolute(root)
    && argv[4] === "--root"
    && argv[5] === path.resolve(root)
    && argv[6] === "--event"
    && Object.hasOwn(MANAGED_HOOK_POLICY_SEQUENCES, argv[7])
  ) {
    return `@${argv[7]}`;
  }
  // Exact invocations of the owned launcher remain managed after Python migration.
  const legacySharedLauncher = (
    argv
    && path.isAbsolute(argv[0])
    && argv[1] === "-I"
    && argv[2] === invocation.launcher
  );
  if (
    legacySharedLauncher
    && argv.length === 5
    && argv[3] === "--event"
    && Object.hasOwn(MANAGED_HOOK_POLICY_SEQUENCES, argv[4])
  ) {
    return `@${argv[4]}`;
  }
  if (
    legacySharedLauncher
    && argv.length === 7
    && typeof root === "string"
    && path.isAbsolute(root)
    && argv[3] === "--root"
    && argv[4] === path.resolve(root)
    && argv[5] === "--event"
    && Object.hasOwn(MANAGED_HOOK_POLICY_SEQUENCES, argv[6])
  ) {
    return `@${argv[6]}`;
  }
  return null;
}


export function codexConfigPath() {
  if (!HOME) {
    return null;
  }
  return path.join(HOME, ".codex", "config.toml");
}

function ompExtensionContentIsKitOwned(current) {
  if (current.toString("utf8").startsWith(OMP_EXTENSION_MARKER_PREFIX)) {
    return true;
  }
  const digest = crypto.createHash("sha256").update(current).digest("hex");
  return LEGACY_OMP_EXTENSION_DIGESTS.has(digest);
}

export function ompExtensionIsKitOwned(target) {
  let handle;
  try {
    handle = fs.openSync(target, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  } catch (error) {
    if (error?.code === "ENOENT") {
      return true;
    }
    return false;
  }
  try {
    if (!fs.fstatSync(handle).isFile()) {
      return false;
    }
    return ompExtensionContentIsKitOwned(fs.readFileSync(handle));
  } finally {
    fs.closeSync(handle);
  }
}

function retireOmpHooksExtension(root) {
  const result = spawnSync(
    fs.existsSync("/usr/bin/python3") ? "/usr/bin/python3" : "/usr/local/bin/python3",
    [
      "-I",
      "-c",
      RETIRE_OMP_EXTENSION_SCRIPT,
      path.resolve(root),
      OMP_EXTENSION_MARKER_PREFIX,
      JSON.stringify([...LEGACY_OMP_EXTENSION_DIGESTS]),
    ],
    { encoding: "utf8", timeout: 30_000 },
  );
  if (result.error || result.status !== 0) {
    const detail = (result.stderr || result.stdout || result.error?.message || "unknown error").trim();
    throw new Error(`cannot safely retire project-local OMP extension: ${detail}`);
  }
  let outcome;
  try {
    outcome = JSON.parse(result.stdout);
  } catch {
    throw new Error("cannot safely retire project-local OMP extension: invalid helper response");
  }
  return outcome;
}

export function removeOmpHooksExtension(root) {
  const target = path.join(root, ".omp", "extensions", "agent-flow-hooks.ts");
  const outcome = retireOmpHooksExtension(root);
  if (outcome.status === "missing") {
    return;
  }
  if (outcome.status === "user-owned") {
    console.warn(`agent-flow: ${path.relative(root, target)} is not kit-managed; leaving it alone.`);
    return;
  }
  if (outcome.status !== "removed") {
    const detail = outcome.detail ? `: ${outcome.detail}` : "";
    throw new Error(`cannot safely retire ${path.relative(root, target)}${detail}`);
  }
  console.log(`  - hooks disabled: removed ${path.relative(root, target)}`);
}


export function removeCodexBroadTrustState(root) {
  const configPath = codexConfigPath();
  if (!configPath || !fs.existsSync(configPath)) {
    return;
  }
  const tableHeader = `[projects."${tomlBasicString(root)}"]`;
  const tableName = tableHeader.slice(1, -1);
  const tablePattern = new RegExp(
    `(^|\\n)\\s*\\[\\s*${escapeRegex(tableName)}\\s*\\]\\s*(?:#.*)?\\n`
      + "([\\s\\S]*?)(?=\\n\\s*\\[[^\\n]+\\]|$)",
  );
  const trustPattern = /(^|\n)\s*trust_level\s*=\s*"trusted"\s*(?:#.*)?(?=\n|$)/;
  const current = fs.readFileSync(configPath, "utf8");
  const next = current.replace(tablePattern, (full, leading, body) => {
    if (!trustPattern.test(body)) {
      return full;
    }
    const kept = body.replace(trustPattern, "$1");
    if (!kept.trim()) {
      return leading;
    }
    return `${leading}${tableHeader}\n${kept.replace(/^\n/, "")}`;
  });
  if (next !== current) {
    fs.writeFileSync(configPath, next.endsWith("\n") ? next : `${next}\n`, "utf8");
  }
}

export function safeSkillName(value) {
  const candidate = String(value).trim();
  return /^[A-Za-z0-9._-]+$/.test(candidate) && !candidate.startsWith(".") && !candidate.includes("..") && candidate !== "."
    ? candidate
    : String(candidate || "skill")
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "") || "skill";
}

export function skillRequires(name) {
  return SKILL_DEPENDENCIES.get(name) || [];
}

export function readJsonIfExists(pathName) {
  if (!fs.existsSync(pathName)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

export function retiredHookScripts(_hooksDisabled) {
  return [...RETIRED_MANAGED_HOOK_SCRIPTS, ...MANAGED_HOOK_SCRIPTS];
}

export function isRetiredHookCommand(command, hooksDisabled, root) {
  const argv = literalShellArgv(command);
  if (hooksDisabled && managedHookScriptName(command, root)) {
    return true;
  }
  return directHookScriptName(argv, root, retiredHookScripts(hooksDisabled)) !== null;
}

export function pruneRetiredHooks(settings, replaceManaged, hooksDisabled, root) {
  if (!settings || typeof settings !== "object" || !settings.hooks) {
    return false;
  }
  let changed = false;
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) {
      continue;
    }
    for (const entry of entries) {
      if (!Array.isArray(entry?.hooks)) {
        continue;
      }
      const kept = entry.hooks.filter(
        (hook) => !isRetiredHookCommand(hook?.command, hooksDisabled, root)
          && !(replaceManaged && managedHookScriptName(hook?.command, root)),
      );
      if (kept.length !== entry.hooks.length) {
        entry.hooks = kept;
        changed = true;
      }
    }
    const nonEmpty = entries.filter((entry) => !Array.isArray(entry?.hooks) || entry.hooks.length > 0);
    if (nonEmpty.length !== entries.length) {
      settings.hooks[event] = nonEmpty;
      changed = true;
    }
  }
  return changed;
}

const LEGACY_RETIRED_HOOK_DIGESTS = new Map([
  ["bind-host-worktree.py", new Set([
    "9e68055544a56b98534ff36b66f5e4caaa699b6a5e0e1ecfb861419616ae7fe4",
    "e3762ce2b9ce712ac8e939f8be2adc33c1a8cd5b8328f0deba0ac3b8837f962c",
  ])],
  ["guard-protected-branch.sh", new Set([
    "d6d7af001d5619b32f791ea3f0f9bc7b09882c443b18fd097ec2b984170bba4b",
    "5c865eae02d27259b1b979cf2c29bda9c96a5afaea52cadda6b952c3afe8c699",
  ])],
  ["guard-host-worktree.sh", new Set([
    "2140425a3b15831298266e17a39066ddfe7f2685292b9b99a9f53cce37c1ce43",
    "9278b9178703905af052855f484a7816b9ee877572146495c1f72630435cf6fc",
  ])],
  ["show-phase-status.sh", new Set([
    "bed53c6a74b28751e2a8848fc03de626c812077ee504932b00649c79cdd27ee6",
    "7703f04e2f82655f6b52dd9c8ec9d02b3c0db538728e7bda86a923ba384ee09f",
  ])],
  ["comment-checker.py", new Set([
    "eed28c0ad23a215930375d2246239a51d608a3a17dc64ecfeb0ab3089e3537ff",
    "6b544b401989cb32bc6cf79857fb0eaa71452b7300380ba00d0835d8d82fc228",
  ])],
  ["record-skill-read.py", new Set([
    "39207371a5b570163d73e730a367967bd6f572466a6c97f204468fcc2dbe6bf0",
    "b7651dc5a45f05e2fcc5c1e635af01453d89a1e1a4617466311eb865b7d4709d",
  ])],
  ["record-command-run.py", new Set([
    "ea40856058a78228c18bfe31de8b12f0c7ca6ae58611d1f51b25ed49609fbe79",
    "4d10acadb66657a9a1b4af32de2cc2b6e9020d939c0d3afb0de828084ffb2114",
  ])],
  ["worktree-tripwire.py", new Set([
    "d8a4b272de46cc8c0440c3432377fa589b483f2fbbe545e1d646c436e83aae46",
    "c3fa61c6f636ec6c75555a9ee8dd837f111392a89320d8d24ae8db3bd217034e",
  ])],
  ["guard-worktree.sh", new Set([
    "00bed85bdf0dc29058d4a3b5e5aaa1c8f432052646810f7f481d5be20d90f89d",
    "43d8e0baecc9595c7caba6c421d35f396c5d7c5e534ae17cdd9d8a2d15e47378",
    "5c0fe25e08e5edd7653c3fd30cc50b4ee2cb57d881ae0e0a26971f293cb41658",
  ])],
  ["prepare-spec-user-prompt.py", new Set([
    "039a8d9b7ba828baae6bb52ef7f95d0c8f3e1b36a56f143c7226c5ddf5444b79",
    "b67f39d80710aeba392974c5aa634a1ff09506f3a920a3090c1e90b667ee6fd3",
    "d018dc728de3e738303daed8bd07adc118bab166ebe815f23cc9695cf8ac98aa",
  ])],
  ["confirm-spec-user-prompt.py", new Set([
    "b3d702557c35e8167e588b5b95cdcc715f2f507bbe7e717e192bb592830cd961",
    "dfb8cfa283b4893052d80e8dea39f7f4d82996b92d4c14e5842e985d09019650",
  ])],
  ["guard-spec-approval.sh", new Set([
    "c2d03292bdf1f7e35c21363643f3d2e7452e63c08db63ecc7de04c2a080a2a50",
  ])],
]);

export function pruneRetiredHookScripts(
  root,
  hooksDisabled,
  forceManaged = false,
  recordedAssets = new Map(),
) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  const targets = [
    ...retiredHookScripts(hooksDisabled)
      .map((scriptName) => path.join(hooksDir, scriptName)),
    path.join(root, ".agent-flow", "scripts", "hook-runtime", "agent-flow-hook.py"),
  ];
  const owned = [];
  const forced = [];
  for (const target of targets) {
    let identity;
    try {
      identity = fs.lstatSync(target);
    } catch (error) {
      if (error?.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    const relative = path.relative(root, target);
    if (!identity.isFile() || identity.isSymbolicLink()) {
      console.warn(`warning: preserved non-regular retired hook: ${relative}`);
      continue;
    }
    const digest = contentHash(fs.readFileSync(target));
    const recordedDigest = recordedAssets instanceof Map
      ? recordedAssets.get(relative.replaceAll("\\", "/"))
      : null;
    const source = path.join(KIT_ROOT, "scripts", "hooks", path.basename(target));
    const currentDigest = fs.existsSync(source)
      ? contentHash(fs.readFileSync(source))
      : null;
    const knownDigest = (
      LEGACY_RETIRED_HOOK_DIGESTS.get(path.basename(target))?.has(digest)
      || currentDigest === digest
    );
    if (knownDigest || recordedDigest === digest) {
      owned.push(target);
    } else if (forceManaged) {
      forced.push(target);
    } else {
      console.warn(`warning: preserved user-edited retired hook: ${relative}`);
    }
  }
  backupAndRemoveFiles(
    root,
    forced,
    (target) => (
      `  - removed retired hook: ${path.relative(root, target)} `
      + `(backup: ${path.relative(root, target)}.removed)`
    ),
  );
  for (const target of owned) {
    fs.rmSync(target);
    console.log(`  - removed retired hook: ${path.relative(root, target)}`);
  }
}

export function mergeHookSettings(settings, desired, hooksDisabled, root) {
  if (!settings.hooks) {
    settings.hooks = {};
  }
  pruneRetiredHooks(settings, true, hooksDisabled, root);
  for (const [event, entries] of Object.entries(desired)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = [];
    }
    for (const entry of entries) {
      const existing = settings.hooks[event].find((e) => (e.matcher ?? "") === (entry.matcher ?? ""));
      if (existing) {
        if (!existing.hooks) {
          existing.hooks = [];
        }
        for (const hook of entry.hooks) {
          const scriptName = managedHookScriptName(hook.command, root);
          const matchingHook = existing.hooks.find(
            (h) => scriptName && managedHookScriptName(h.command, root) === scriptName,
          );
          if (matchingHook) {
            Object.assign(matchingHook, hook);
          } else if (!existing.hooks.some((h) => h.command === hook.command)) {
            existing.hooks.push(hook);
          }
        }
      } else {
        settings.hooks[event].push(entry);
      }
    }
  }
}

export function mergeHookConfig(settings, source, hooksDisabled, root) {
  if (!source || typeof source !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(source)) {
    if (key !== "hooks" && settings[key] === undefined) {
      settings[key] = value;
    }
  }
  if (source.hooks) {
    mergeHookSettings(settings, source.hooks, hooksDisabled, root);
  }
}

function managedHooksSettings(root) {
  return {
    hooks: Object.fromEntries(
      Object.keys(MANAGED_HOOK_POLICY_SEQUENCES)
        .filter((eventName) => eventName !== "context")
        .map((eventName) => [
          eventName,
          [{
            matcher: MANAGED_HOOK_POLICY_SEQUENCES[eventName].matcher,
            hooks: [{
              type: "command",
              command: hookEventCommand(root, eventName),
            }],
          }],
        ]),
    ),
  };
}
export function claudeHooksSettings(root) {
  return managedHooksSettings(root);
}

export function codexHooksSettings(root) {
  return managedHooksSettings(root);
}

function assertBooleanInstallOption(name, value) {
  if (typeof value !== "boolean") {
    throw new TypeError(`${name} must be a boolean`);
  }
}

export function installCodexHooks(root, { hooksDisabled }) {
  assertBooleanInstallOption("hooksDisabled", hooksDisabled);
  const settingsPaths = [
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
  ];
  for (const settingsPath of settingsPaths) {
    if (pathHasSymlink(root, settingsPath)) {
      throw new Error(`refusing symlinked project hook registration: ${settingsPath}`);
    }
  }
  const settings = {};
  for (const settingsPath of settingsPaths) {
    mergeHookConfig(settings, readHookSettings(settingsPath), hooksDisabled, root);
  }
  mergeHookSettings(settings, codexHooksSettings(root).hooks, hooksDisabled, root);
  for (const settingsPath of settingsPaths) {
    writeAtomicTextFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
  }
  return true;
}

export function installClaudeHooks(root, { hooksDisabled }) {
  assertBooleanInstallOption("hooksDisabled", hooksDisabled);
  const settingsPath = path.join(root, ".claude", "settings.json");
  if (pathHasSymlink(root, settingsPath)) {
    throw new Error(`refusing symlinked project hook registration: ${settingsPath}`);
  }
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks, hooksDisabled, root);
  writeAtomicTextFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
}

export function installOmpHooks(home, { force }) {
  assertBooleanInstallOption("force", force);
  const target = path.join(home, ".omp", "agent", "extensions", "agent-flow-hooks.ts");
  const result = recordOmpAdapter({
    adapterPath: target,
    content: ompHooksExtensionSource(),
    force,
  });
  return result.installed;
}

export function installGlobalHookRegistrations(home, { force, hooksDisabled }) {
  const ompInstalled = installOmpHooks(home, { force });
  const codexInstalled = withSharedHookMutation(() => {
    const installed = installCodexHooks(home, { hooksDisabled });
    installClaudeHooks(home, { hooksDisabled });
    return installed;
  });
  return { ompInstalled, codexInstalled };
}


export function skillIndexBlock(root) {
  const index = readJsonIfExists(path.join(root, ".agent-flow", "skills", "index.json"));
  const skills = Array.isArray(index?.skills) ? index.skills : [];
  if (skills.length === 0) {
    // 인덱스가 없는 설치본에서 거짓 목록을 쓰지 않는다. 빈 인덱스는 "아직
    // 모른다"이지 "skill이 없다"가 아니다.
    return [
      SKILL_INDEX_START,
      `- 설치된 skill 인덱스가 아직 없다. \`${AGENT_FLOW_COMMAND} skills sync\` 후 다시 생성된다.`,
      SKILL_INDEX_END,
    ].join("\n");
  }
  const names = (delivery) =>
    skills
      .filter((skill) => (skill.delivery === "passive") === (delivery === "passive"))
      .map((skill) => String(skill.name))
      .sort((a, b) => a.localeCompare(b));
  const lines = [
    SKILL_INDEX_START,
    "```text",
    "[agent-flow skill index]|root: .agent-flow/skills",
    "|IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, scope가 걸리는 것만 읽는다.",
  ];
  const passive = names("passive");
  if (passive.length > 0) {
    lines.push(`|always:{${passive.join(",")}}`);
  }
  const onDemand = names("on-demand");
  if (onDemand.length > 0) {
    lines.push(`|on-demand:{${onDemand.join(",")}}`);
  }
  lines.push("```", SKILL_INDEX_END);
  return lines.join("\n");
}

export function upsertSkillIndexBlock(root) {
  const block = skillIndexBlock(root);
  for (const fileName of ["AGENTS.md", "CLAUDE.md"]) {
    const target = path.join(root, fileName);
    if (!fs.existsSync(target)) continue;
    const current = fs.readFileSync(target, "utf8");
    const start = current.indexOf(SKILL_INDEX_START);
    const end = current.indexOf(SKILL_INDEX_END);
    if (start === -1 || end === -1 || end < start) continue;
    const next = current.slice(0, start) + block + current.slice(end + SKILL_INDEX_END.length);
    if (next !== current) {
      fs.writeFileSync(target, next, "utf8");
    }
  }
}
