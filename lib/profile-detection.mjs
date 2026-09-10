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
    if (/\bid\s*(?:\(\s*)?["']org\.springframework\.boot["']/.test(gradle)
        || /["']org\.springframework\.boot:spring-boot[^"']*["']/.test(gradle)
        || /<groupId\b[^>]*>\s*org\.springframework\.boot\s*<\/groupId>/.test(pom)) candidates.add("spring");
    if (/\bid\s*(?:\(\s*)?["']io\.ktor(?:\.plugin)?["']/.test(gradle)
        || /["']io\.ktor:ktor-server-[^"']+["']/.test(gradle)
        || /<(dependency|plugin)\b[^>]*>(?:(?!<\/\1>)[\s\S])*<groupId>\s*io\.ktor\s*<\/groupId>\s*<artifactId>\s*ktor-server-[^<]+<\/artifactId>/.test(pom)) candidates.add("ktor");
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
