// 두 install 진입점이 공유하는 순수 헬퍼. 예전에는 같은 본문이 양쪽에 한 벌씩
// 있었고, 그래서 둘이 갈라지지 않았는지 확인하는 검사가 따로 필요했다.
//
// 여기 있는 것은 모듈 상태에 기대지 않는 함수들뿐이다. `installCodexHooks`처럼
// 각 진입점의 전역(`PROJECT`, `AF_DIR`)을 읽는 본문은 아직 각자 갖고 있다.
//
// `hooksDisabled`는 진입점마다 다른 시점에 정해지므로 인자로 받는다. 기본값을 두면
// 인자를 빠뜨린 호출이 hook을 켜 둔 것으로 조용히 처리된다.

import crypto from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { MANAGED_HOOK_SCRIPTS, RETIRED_MANAGED_HOOK_SCRIPTS } from "./managed-hooks.mjs";
import { OMP_EXTENSION_MARKER } from "./omp-hooks-extension.mjs";
import { SKILL_DEPENDENCIES } from "./skill-selection.mjs";

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
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, `${JSON.stringify({ version: 1, files }, null, 2)}\n`, "utf8");
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

export function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  for (const entry of fs.readdirSync(hooksDir)) {
    if (entry.endsWith(".sh") || entry.endsWith(".py")) {
      fs.chmodSync(path.join(hooksDir, entry), 0o755);
    }
  }
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

export function pruneRetiredManagedScripts(root) {
  const scriptsDir = path.join(root, ".agent-flow", "scripts");
  for (const scriptName of ["check-context-docs.mjs", "check-context-docs.ts"]) {
    const target = path.join(scriptsDir, scriptName);
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
      continue;
    }
    const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
    if (kept !== null) {
      fs.copyFileSync(target, kept);
      fs.chmodSync(kept, 0o644);
    }
    fs.rmSync(target, { force: true });
    console.log(`  - removed retired script: ${path.relative(root, target)}`);
  }
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

const HOST_WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

export function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Run two independent reviewer subprocesses from the Claude/Codex pool before approve. Each reviewer section must include \`reviewer-source: sub-agent\`. OMP and controller-session work are never reviewer providers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\ncodex-claude-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
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

// 프로젝트에 설치하는 managed Python CLI 진입점. Node wrapper와 사용자가 동일한
// runtime을 타도록 한 경로로 고정한다.
export const PROJECT_LAUNCHER_RELATIVE = path.join(".agent-flow", "bin", "agent-flow");

// 실행할 인터프리터는 install 시점에 한 번 정해 절대경로로 못박는다.
// launcher가 실행 시점에 후보를 탐색하면 `$root/.venv/bin/python`이나 `$PYTHON`으로
// managed runtime을 바꿀 수 있다. 다른 managed hook을 `/usr/bin/python3 -I`로
// 못박는 것과 같은 이유다.
//
// 플래그도 함께 정한다. `-I`는 user-site와 cwd를 통째로 끊으므로 우선이지만,
// PyYAML이 user-site에만 있는 배치(`/usr/bin/python3`이 그렇다)에서는 `-I`로
// import가 실패한다. 그런 경우에만 `-E`로 내려간다 — 그때는 user-site가 살아
// 있어 `usercustomize`가 이 프로세스에서 실행될 수 있다는 것이 대가다.
//
// 인터프리터 target은 realpath로 별도 기록한다. venv executable 자체를 realpath로
// 바꾸면 `pyvenv.cfg` 탐색이 끊겨 install 때 통과한 dependency를 실행 때 잃는다.
let managedPythonCache = null;

export function resolveManagedPython() {
  if (managedPythonCache !== null) {
    return managedPythonCache;
  }
  const virtualEnv = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, "bin", "python")
    : null;
  const candidates = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
    virtualEnv,
    path.join(KIT_ROOT, ".venv", "bin", "python"),
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
    "/usr/bin/python3",
  ].filter(Boolean);
  // `-I` 가능한 후보를 한 바퀴 먼저 본다. 후보 순서가 아니라 격리 여부가 먼저다 —
  // 앞선 후보 때문에 `-E`로 내려가면 그 프로젝트는 영구히 user-site에 노출된다.
  for (const flag of ["-I", "-E"]) {
    for (const candidate of candidates) {
      // 실행 시점과 **같은 플래그**로 검사한다. 플래그 없이 통과한 인터프리터는
      // PYTHONPATH나 user-site로만 yaml을 얻고 있을 수 있어 launcher에서 다시 죽는다.
      const probe = spawnSync(
        candidate,
        [
          flag,
          "-c",
          "import json, os, sys, yaml; sys.stdout.write(json.dumps({'python': os.path.abspath(sys.executable), 'realpath': os.path.realpath(sys.executable)}))",
        ],
        { encoding: "utf8", timeout: 15_000 },
      );
      if (probe.error || probe.status !== 0) {
        continue;
      }
      let identity;
      try {
        identity = JSON.parse(probe.stdout || "");
      } catch {
        continue;
      }
      const executable = identity?.python;
      const resolved = identity?.realpath;
      if (
        typeof executable === "string"
        && path.isAbsolute(executable)
        && typeof resolved === "string"
        && path.isAbsolute(resolved)
        && fs.existsSync(executable)
        && fs.existsSync(resolved)
      ) {
        managedPythonCache = { python: executable, realpath: resolved, flag };
        return managedPythonCache;
      }
    }
  }
  throw new Error(
    "no Python with PyYAML found for the managed launcher. Install it (pip install pyyaml) "
    + `or point PYTHON at an interpreter that has it. Tried: ${candidates.join(", ")}`,
  );
}

// 내용은 프로젝트 경로에 의존하지 않는다(root는 `$0`에서 유도). 박히는 것은 install
// 시점에 해석된 인터프리터와 그 플래그뿐이라, 그 해석이 같은 머신에서도 install
// 프로세스의 env(`PYTHON`, `VIRTUAL_ENV`)에 따라 갈리면 바이트도 갈린다.
export function projectLauncherSource(managedPython) {
  const pythonPath = managedPython?.python;
  const flag = managedPython?.flag;
  if (!pythonPath || !path.isAbsolute(pythonPath)) {
    throw new Error("managed launcher needs an absolute python path");
  }
  if (flag !== "-I" && flag !== "-E") {
    throw new Error(`managed launcher needs an isolation flag, got: ${flag}`);
  }
  if (pythonPath.includes("'")) {
    throw new Error(`managed python path cannot contain a quote: ${pythonPath}`);
  }
  return `#!/bin/sh
# agent-flow: managed project launcher. Created by \`agent-flow-kit install\`.
# Do not edit — the run-start hook integrity gate compares this file's digest.
set -u
# 로더 주입은 \`-I\`/\`-E\`가 막지 못한다. 승인 hook이 타는 유일한 실행 경로라 끊는다.
# fallback/versioned 변종을 빼면 같은 자리가 그대로 다시 열린다.
unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH DYLD_VERSIONED_LIBRARY_PATH DYLD_VERSIONED_FRAMEWORK_PATH
launcher_dir=\${0%/*}
if [ "$launcher_dir" = "$0" ]; then
  launcher_dir=.
fi
root=$(CDPATH= cd -- "$launcher_dir/../.." && pwd -P) || exit 127
runtime="$root/.agent-flow/runtime/python"
if [ ! -f "$runtime/agent_flow/cli.py" ]; then
  printf 'agent-flow: managed python runtime is missing at %s; run \`agent-flow-kit install\` from the leader checkout\\n' "$runtime" >&2
  exit 127
fi
python='${pythonPath}'
if [ ! -x "$python" ]; then
  printf 'agent-flow: managed python %s is missing; run \`agent-flow-kit install\` from the leader checkout\\n' "$python" >&2
  exit 127
fi
# sys.path에서 cwd를 **지운다**. \`-c\`는 sys.path[0]에 cwd를 넣으므로, 끼워 넣기만
# 하면 프로젝트에 놓인 argparse.py 하나가 승인 프로세스 안에서 실행된다.
exec "$python" ${flag} -c 'import os, sys; sys.path = [sys.argv.pop(1)] + [entry for entry in sys.path if entry not in ("", ".", os.getcwd())]; from agent_flow.cli import main; raise SystemExit(main())' "$runtime" "$@"
`;
}

// 기록하는 digest는 **디스크에 심은 그 파일**에서 뽑는다. 인터프리터 해석이
// 진입점마다 갈라져도 kit.json과 파일이 어긋나 런이 막히는 일은 없어야 한다.
export function projectLauncherDigest(root) {
  const target = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  try {
    return crypto.createHash("sha256").update(fs.readFileSync(target)).digest("hex");
  } catch {
    return null;
  }
}

// 게이트가 launcher만 대조하면 그가 exec하는 인터프리터는 무검증으로 남는다.
// 경로와 digest를 함께 기록해 그 자리도 오라클을 갖게 한다.
export function projectLauncherPythonRecord() {
  let managed;
  try {
    managed = resolveManagedPython();
  } catch {
    return null;
  }
  try {
    return {
      path: managed.python,
      flag: managed.flag,
      realpath: managed.realpath,
      sha256: crypto
        .createHash("sha256")
        .update(fs.readFileSync(managed.python))
        .digest("hex"),
    };
  } catch {
    return null;
  }
}

// mode는 0o755로 고정한다. hook이 group/other write 비트가 붙은 launcher를
// 거부하므로(가로채기 방지) umask에 맡기면 환경에 따라 조용히 죽는다.
export function installProjectLauncher(root) {
  const target = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  let source;
  try {
    source = projectLauncherSource(resolveManagedPython());
  } catch (error) {
    // 조용히 넘기면 채팅 승인만 죽은 설치본이 된다. 런 시작 게이트도 digest 없음으로
    // 막지만, 원인을 아는 것은 이 자리뿐이다.
    console.warn(
      `agent-flow: managed launcher not installed: ${error instanceof Error ? error.message : String(error)}`,
    );
    return null;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  // 게이트는 `lstat`으로 판정한다. symlink를 그대로 두면 재설치로도 복구되지 않아
  // 사용자가 손으로 지울 때까지 어떤 run도 시작하지 못한다.
  const identity = fs.lstatSync(target, { throwIfNoEntry: false });
  if (identity && !identity.isFile()) {
    fs.rmSync(target, { recursive: true, force: true });
  } else if (identity && fs.readFileSync(target, "utf8") === source) {
    fs.chmodSync(target, 0o755);
    return target;
  }
  // 제자리 truncate로 쓰면 install 중에 도는 hook이 반쪽 파일을 해시하거나 exec해
  // 그 프롬프트의 승인이 조용히 사라진다. 같은 디렉터리에 쓰고 rename한다.
  const staging = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(staging, source, { mode: 0o755 });
  fs.chmodSync(staging, 0o755);
  fs.renameSync(staging, target);
  return target;
}
export function syncManagedWorktreeHostHooks(root) {
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  const identity = fs.lstatSync(launcher, { throwIfNoEntry: false });
  if (!identity?.isFile()) {
    return false;
  }
  const topLevel = gitOutput(root, ["rev-parse", "--show-toplevel"]);
  if (topLevel === null) {
    const gitMarker = fs.lstatSync(path.join(root, ".git"), { throwIfNoEntry: false });
    if (gitMarker !== undefined) {
      throw new Error("cannot resolve repository root before host hook sync");
    }
    return true;
  }
  const worktrees = gitOutput(root, ["worktree", "list", "--porcelain", "-z"]);
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
  const result = spawnSync(
    launcher,
    ["worktree", "sync-host-hooks", "--root", root],
    {
      cwd: root,
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


export function hookScriptCommand(root, scriptName) {
  const scriptPath = shellQuote(path.join(root, ".agent-flow", "scripts", "hooks", scriptName));
  if (scriptName.endsWith(".py")) {
    return `/usr/bin/python3 -I ${scriptPath}`;
  }
  return `/bin/bash ${scriptPath}`;
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

export function managedHookScriptName(command) {
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of MANAGED_HOOK_SCRIPTS) {
    if (
      normalized === `.agent-flow/scripts/hooks/${scriptName}` ||
      normalized === `scripts/hooks/${scriptName}` ||
      normalized.endsWith(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.endsWith(`/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/scripts/hooks/${scriptName}`)
    ) {
      return scriptName;
    }
  }
  return null;
}

export function managedHookDigests() {
  return Object.fromEntries(
    MANAGED_HOOK_SCRIPTS.map((name) => [
      name,
      crypto
        .createHash("sha256")
        .update(fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", name)))
        .digest("hex"),
    ]),
  );
}

export function codexConfigPath() {
  if (!HOME) {
    return null;
  }
  return path.join(HOME, ".codex", "config.toml");
}

export function ompExtensionIsKitOwned(target) {
  if (!fs.existsSync(target)) {
    return true;
  }
  const current = fs.readFileSync(target, "utf8");
  // 표식은 이번 버전부터 붙는다. 그 이전 설치본에는 없으므로 생성 서명으로도
  // 인정한다. 이게 없으면 기존 사용자는 첫 업그레이드에서 영영 막힌다.
  return (
    current.includes(OMP_EXTENSION_MARKER) ||
    current.includes("export default function agentFlowHooks(")
  );
}

export function removeOmpHooksExtension(root) {
  const target = path.join(root, ".omp", "extensions", "agent-flow-hooks.ts");
  if (!fs.existsSync(target)) {
    return;
  }
  if (!ompExtensionIsKitOwned(target)) {
    console.warn(`agent-flow: ${path.relative(root, target)} is not kit-managed; leaving it alone.`);
    return;
  }
  const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
  if (kept !== null) {
    fs.copyFileSync(target, kept);
  }
  fs.rmSync(target, { force: true });
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

export function retiredHookScripts(hooksDisabled) {
  return hooksDisabled
    ? [...RETIRED_MANAGED_HOOK_SCRIPTS, ...MANAGED_HOOK_SCRIPTS]
    : RETIRED_MANAGED_HOOK_SCRIPTS;
}

export function isRetiredHookCommand(command, hooksDisabled) {
  if (typeof command !== "string" || !command) {
    return false;
  }
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  return retiredHookScripts(hooksDisabled).some(
    (name) => normalized.endsWith(`/scripts/hooks/${name}`) || normalized === `scripts/hooks/${name}`,
  );
}

export function pruneRetiredHooks(settings, replaceManaged, hooksDisabled) {
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
        (hook) => !isRetiredHookCommand(hook?.command, hooksDisabled)
          && !(replaceManaged && managedHookScriptName(hook?.command)),
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

export function pruneRetiredHookScripts(root, hooksDisabled) {
  // 설정에서만 빼면 실행 파일이 디스크에 남는다. 남은 파일을 다른 경로가
  // 다시 집어 실행하면 은퇴시킨 guard가 되살아난다.
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  for (const scriptName of retiredHookScripts(hooksDisabled)) {
    const target = path.join(hooksDir, scriptName);
    if (fs.existsSync(target)) {
      // 사용자가 같은 이름으로 자기 스크립트를 뒀을 수 있다. 되돌릴 수 있게
      // 사본을 남기고 지운다. 설치본이 관리하지 않는 host 설정이 이 경로를
      // 여전히 가리킬 수 있으므로 경로를 함께 알린다.
      const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
      if (kept !== null) {
        fs.copyFileSync(target, kept);
        // 실행 권한은 떼어 둔다. 되살릴 수 있게 남기는 사본이지 실행 대상이 아니다.
        fs.chmodSync(kept, 0o644);
      }
      fs.rmSync(target, { force: true });
      console.log(
        `  - removed retired hook: ${path.relative(root, target)} ` +
          `(backup: ${path.relative(root, target)}.removed)`,
      );
    }
  }
}

export function mergeHookSettings(settings, desired, hooksDisabled) {
  if (!settings.hooks) {
    settings.hooks = {};
  }
  pruneRetiredHooks(settings, true, hooksDisabled);
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
          const scriptName = managedHookScriptName(hook.command);
          const matchingHook = existing.hooks.find(
            (h) => scriptName && managedHookScriptName(h.command) === scriptName,
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

export function mergeHookConfig(settings, source, hooksDisabled) {
  if (!source || typeof source !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(source)) {
    if (key !== "hooks" && settings[key] === undefined) {
      settings[key] = value;
    }
  }
  if (source.hooks) {
    mergeHookSettings(settings, source.hooks, hooksDisabled);
  }
}

export function claudeHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
        {
          matcher: HOST_WRITE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "comment-checker.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
        {
          matcher: SKILL_USE_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
        },
        {
          matcher: COMMAND_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "record-command-run.py") },
            { type: "command", command: hookScriptCommand(root, "bind-host-worktree.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "worktree-tripwire.py") },
          ],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

export function codexHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
        {
          matcher: HOST_WRITE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "comment-checker.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
        {
          matcher: SKILL_USE_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
        },
        {
          matcher: COMMAND_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "record-command-run.py") },
            { type: "command", command: hookScriptCommand(root, "bind-host-worktree.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "worktree-tripwire.py") },
          ],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
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
