import fs from "node:fs";
import path from "node:path";

const GRADLE_FILES = ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"];
const FLUTTER_SDK_DEPENDENCY_RE = /^\s*sdk:\s*["']?flutter["']?(?:[ \t]+#.*)?[ \t]*$/m;

export function packageDependencies(root) {
  const manifest = path.join(root, "package.json");
  if (!fs.existsSync(manifest)) return new Set();
  const text = fs.readFileSync(manifest, "utf8");
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new Error(`invalid package manifest ${manifest}: ${error.message}`, { cause: error });
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error(`${manifest} must contain a JSON object`);
  }
  return new Set(["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]
    .flatMap((key) => payload[key] && typeof payload[key] === "object" && !Array.isArray(payload[key])
      ? Object.keys(payload[key]) : []));
}

function withoutGradleComments(text) {
  return text.replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    (match) => match.startsWith("/") ? "" : match);
}

function readIfFile(file) {
  return fs.existsSync(file) && fs.statSync(file).isFile() ? fs.readFileSync(file, "utf8") : "";
}

function buildDirectories(root) {
  const canonicalRoot = fs.realpathSync(root);
  const directories = new Set([canonicalRoot]);
  for (const directory of directories) {
    const settings = ["settings.gradle", "settings.gradle.kts"]
      .map((name) => withoutGradleComments(readIfFile(path.join(directory, name))))
      .join("\n");
    const children = [];
    for (const include of settings.matchAll(/\binclude\s*(?:\(([^)]*)\)|([^\n]+))/g)) {
      for (const name of (include[1] ?? include[2]).matchAll(/["'](:?[A-Za-z0-9_.:-]+)["']/g)) {
        children.push(name[1].replace(/^:/, "").replaceAll(":", "/"));
      }
    }
    const pom = readIfFile(path.join(directory, "pom.xml")).replace(/<!--[\s\S]*?-->/g, "");
    for (const modules of pom.matchAll(/<modules\b[^>]*>([\s\S]*?)<\/modules>/g)) {
      for (const module of modules[1].matchAll(/<module\b[^>]*>\s*([^<]+?)\s*<\/module>/g)) children.push(module[1]);
    }
    for (const child of children) {
      const candidate = path.resolve(directory, child);
      if (!fs.existsSync(candidate) || !fs.lstatSync(candidate).isDirectory()) continue;
      const resolved = fs.realpathSync(candidate);
      const relative = path.relative(canonicalRoot, resolved);
      if (relative && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)) {
        directories.add(resolved);
      }
    }
  }
  return directories;
}

function hasSpringBootEvidence(gradle, pom) {
  return /\bid\s*(?:\(\s*)?["']org\.springframework\.boot["']/.test(gradle)
    || /["']org\.springframework\.boot:spring-boot[^"']*["']/.test(gradle)
    || /<groupId\b[^>]*>\s*org\.springframework\.boot\s*<\/groupId>/.test(pom);
}

function hasKtorServerEvidence(gradle, pom) {
  return /\bid\s*(?:\(\s*)?["']io\.ktor(?:\.plugin)?["']/.test(gradle)
    || /["']io\.ktor:ktor-server-[^"']+["']/.test(gradle)
    || /<(dependency|plugin)\b[^>]*>(?:(?!<\/\1>)[\s\S])*<groupId>\s*io\.ktor\s*<\/groupId>\s*<artifactId>\s*ktor-server-[^<]+<\/artifactId>/.test(pom);
}

// Kotlin 컴파일 plugin 적용. `kotlin("stdlib")`나 `org.jetbrains.kotlin:*` 의존성은 Java 서비스도 쓴다.
const KOTLIN_GRADLE_PLUGIN_RE = /\bkotlin\s*\(\s*["'](?:jvm|android|multiplatform|kapt|plugin\.[\w.-]+)["']\s*\)|["']org\.jetbrains\.kotlin\.(?:jvm|android|multiplatform|kapt|plugin\.[\w.-]+)["']|\bapply\s*\(?\s*plugin\s*[:=]\s*["'](?:kotlin(?:-[\w-]+)?|org\.jetbrains\.kotlin\.[\w.-]+)["']/;

// 빌드 산출물·의존성·숨김 디렉터리와 소스 트리 안쪽은 모듈을 찾으러 내려가지 않는다.
const MODULE_WALK_PRUNED = new Set(["node_modules", "build", "out", "target", "dist", "src"]);

const MODULE_NAME_RE = /^:?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*$/;

// 선언된 모듈이 모두 걷는 범위 안의 디렉터리인가. 걷기가 닿지 않는 모듈(`includeFlat`,
// `projectDir` 재배치, 심볼릭 링크 경유)과 설정의 `apply`가 가져오는 미확인 선언은 거부한다.
// `plugins { ... apply false }`와 `.apply(false)`는 적용이 아니다. 문자열 안의 낱말은 가린다.
function modulesStayInCheckout(directory, root) {
  const settings = ["settings.gradle", "settings.gradle.kts"]
    .map((name) => withoutGradleComments(readIfFile(path.join(directory, name))))
    .join("\n");
  const code = settings.replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g,
    (literal) => literal[0] + " ".repeat(literal.length - 2) + literal[0]);
  if (/\b(?:projectDir|includeFlat)\b|\bapply\b(?!\s*(?:false\b|\(\s*false\s*\)))/.test(code)) return false;
  const modules = [];
  for (const match of code.matchAll(/\binclude\b\s*(\([^)]*\)|[^\n]*)/dg)) {
    const [start, end] = match.indices[1];
    const parts = settings.slice(start, end).replace(/^\(|\)$/g, "").split(",").map((part) => part.trim());
    if (parts.length > 1 && parts.at(-1) === "" && match[1].startsWith("(")) parts.pop();
    for (const part of parts) {
      const name = /^(["'])(.*)\1$/.exec(part)?.[2];
      if (name === undefined || !MODULE_NAME_RE.test(name)) return false;
      modules.push(name.replace(/^:/, "").replaceAll(":", "/"));
    }
  }
  const pom = readIfFile(path.join(directory, "pom.xml")).replace(/<!--[\s\S]*?-->/g, "");
  for (const block of pom.matchAll(/<modules\b[^>]*>([\s\S]*?)<\/modules>/g)) {
    for (const module of block[1].matchAll(/<module\b[^>]*>\s*([^<]+?)\s*<\/module>/g)) modules.push(module[1]);
  }
  return modules.every((module) => {
    const target = path.resolve(directory, module);
    const relative = path.relative(root, target);
    return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
      && relative.split(path.sep).every((segment) => !segment.startsWith(".") && !MODULE_WALK_PRUNED.has(segment))
      && fs.existsSync(target) && fs.realpathSync(target) === target && fs.lstatSync(target).isDirectory();
  });
}

// spring과 ktor profile은 Java 서비스도 받으므로, Kotlin 전용 hexagonal 계약은 체크아웃의 JVM 코드가
// 모두 Kotlin일 때만 준다. 코드는 디렉터리를 직접 걸어 찾는다 — 한정 호출, 중첩 include, 동적 인자를
// 정규식으로 다 읽을 수 없어 모듈 목록만 믿으면 읽지 못한 Java 모듈이 계약을 통과시킨다. 선언은 걷는
// 범위 밖을 가리키지 않는지만 본다. `src/main/java`(Groovy·Scala 포함)가 있거나 Spring Boot·Ktor를
// 적용한 부모 아닌 빌드는 Kotlin 근거가 있어야 한다. `apply false`/`.apply(false)` 선언은 적용이
// 아니고, `.kts` 빌드 스크립트는 Java 서비스도 쓴다.
export function isKotlinBackendProject(root) {
  try {
    const canonicalRoot = fs.realpathSync(root);
    let kotlin = false;
    const pending = [canonicalRoot];
    while (pending.length > 0) {
      const directory = pending.pop();
      if (!modulesStayInCheckout(directory, canonicalRoot)) return false;
      const gradle = ["build.gradle", "build.gradle.kts"]
        .map((name) => withoutGradleComments(readIfFile(path.join(directory, name))))
        .join("\n")
        .replace(/^[^\n]*(?:\n[ \t]*\.[^\n]*)*\bapply\s*(?:\(\s*false\s*\)|false\b)[^\n]*$/gm, "");
      const pom = readIfFile(path.join(directory, "pom.xml")).replace(/<!--[\s\S]*?-->/g, "");
      const main = path.join(directory, "src", "main");
      const kotlinModule = fs.existsSync(path.join(main, "kotlin"))
        || KOTLIN_GRADLE_PLUGIN_RE.test(gradle)
        || /<artifactId>\s*kotlin-maven-plugin\s*<\/artifactId>/.test(pom);
      const jvmCode = ["java", "groovy", "scala"].some((name) => fs.existsSync(path.join(main, name)));
      const framework = hasSpringBootEvidence(gradle, pom) || hasKtorServerEvidence(gradle, pom);
      const aggregator = /<packaging>\s*pom\s*<\/packaging>/.test(pom);
      if (!kotlinModule && (jvmCode || (framework && !aggregator))) return false;
      kotlin ||= kotlinModule;
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        if (entry.isDirectory() && !entry.name.startsWith(".") && !MODULE_WALK_PRUNED.has(entry.name)) {
          pending.push(path.join(directory, entry.name));
        }
      }
    }
    return kotlin;
  } catch (error) {
    if (error?.code === "EACCES" || error?.code === "EPERM") return false;
    throw error;
  }
}

export function detectProfile(root) {
  const dependencies = packageDependencies(root);
  const candidates = new Set();
  const has = (name) => fs.existsSync(path.join(root, name));
  if (dependencies.has("react-native") || dependencies.has("expo")) candidates.add("react-native");
  if (FLUTTER_SDK_DEPENDENCY_RE.test(readIfFile(path.join(root, "pubspec.yaml")))) candidates.add("flutter");
  if (dependencies.has("next") || ["next.config.js", "next.config.mjs", "next.config.ts"].some(has)) candidates.add("nextjs");
  if (has("Package.swift") || fs.readdirSync(root).some((name) => /\.(?:xcodeproj|xcworkspace)$/.test(name))) candidates.add("ios");
  if (has("pyproject.toml") || has("requirements.txt")) candidates.add("python");
  let jvm = false;
  for (const directory of buildDirectories(root)) {
    const gradle = GRADLE_FILES.map((name) => {
      const file = path.join(directory, name);
      if (fs.existsSync(file)) jvm = true;
      return withoutGradleComments(readIfFile(file));
    }).join("\n");
    const pomPath = path.join(directory, "pom.xml");
    if (fs.existsSync(pomPath)) jvm = true;
    const pom = readIfFile(pomPath).replace(/<!--[\s\S]*?-->/g, "");
    if (hasSpringBootEvidence(gradle, pom)) candidates.add("spring");
    if (hasKtorServerEvidence(gradle, pom)) candidates.add("ktor");
    if (/\bid\s*(?:\(\s*)?["']com\.android\.(?:application|library|test|dynamic-feature)["']/.test(gradle)
        || ["src/main/AndroidManifest.xml", "app/src/main/AndroidManifest.xml"].some((name) => fs.existsSync(path.join(directory, name)))) candidates.add("android");
  }
  if (candidates.has("react-native") || candidates.has("flutter")) candidates.delete("android");
  if (candidates.size > 1) throw new Error(`ambiguous project profiles in ${root}: ${[...candidates].sort().join(", ")}; select an explicit profile`);
  if (candidates.size === 1) return [...candidates][0];
  if (jvm) throw new Error(`JVM framework evidence is insufficient in ${root}; select an explicit profile`);
  if (has("package.json")) return has("tsconfig.json") ? "typescript" : "node";
  return "generic";
}

export function installProfile(root, args, previousIndex) {
  const explicit = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--profile" && args[index + 1]) explicit.push(...args[++index].split(","));
    else if (args[index].startsWith("--profile=")) explicit.push(...args[index].slice(10).split(","));
  }
  const selected = explicit.map((name) => name.trim()).filter(Boolean);
  if (selected.length) return selected[0];
  const previous = previousIndex?.selection;
  if (previous?.mode === "filtered" && previous.profiles?.length) return previous.profiles[0];
  return detectProfile(root);
}
