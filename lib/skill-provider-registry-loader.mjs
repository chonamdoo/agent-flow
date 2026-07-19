import fs from "node:fs";
import path from "node:path";
import { normalizeSkillProviderRegistry } from "./skill-provider-registry.mjs";

export function loadSkillProviderRegistry(registryPath, options = {}) {
  let value;
  try {
    const authorityRoot = options.authorityRoot ?? path.dirname(path.resolve(registryPath));
    const snapshot = readAuthorityFileSnapshot(
      registryPath,
      authorityRoot,
      "skill provider registry",
    );
    value = JSON.parse(snapshot.bytes.toString("utf8"));
  } catch (error) {
    throw new Error(`invalid skill provider registry: ${error instanceof Error ? error.message : String(error)}`);
  }
  return normalizeSkillProviderRegistry(value, options);
}

export function readAuthorityFileSnapshot(pathName, authorityRoot, label = "authority") {
  const directories = pinAuthorityDirectories(authorityRoot, pathName);
  assertAuthorityDirectories(directories, label);
  const initial = fs.lstatSync(pathName, { bigint: true });
  if (initial.isSymbolicLink() || !initial.isFile() || initial.nlink !== 1n) {
    throw new Error(`unsafe ${label} file: ${pathName}`);
  }
  const descriptor = fs.openSync(
    pathName,
    fs.constants.O_RDONLY
      | (fs.constants.O_NOFOLLOW || 0)
      | (fs.constants.O_NONBLOCK || 0),
  );
  try {
    holdAuthorityReadForTest(pathName);
    const before = fs.fstatSync(descriptor, { bigint: true });
    assertAuthorityDirectories(directories, label);
    if (
      !before.isFile()
      || before.nlink !== 1n
      || !sameAuthorityIdentity(initial, before)
    ) {
      throw new Error(`${label} file changed while reading: ${pathName}`);
    }
    const bytes = fs.readFileSync(descriptor);
    const after = fs.fstatSync(descriptor, { bigint: true });
    const current = fs.lstatSync(pathName, { bigint: true });
    assertAuthorityDirectories(directories, label);
    if (
      !after.isFile()
      || !current.isFile()
      || after.nlink !== 1n
      || current.nlink !== 1n
      || !sameAuthorityIdentity(before, after)
      || !sameAuthorityIdentity(after, current)
    ) {
      throw new Error(`${label} file changed while reading: ${pathName}`);
    }
    return { bytes, metadata: before };
  } finally {
    fs.closeSync(descriptor);
  }
}

function pinAuthorityDirectories(authorityRoot, pathName) {
  const authority = path.resolve(authorityRoot);
  const target = path.resolve(pathName);
  const relative = path.relative(authority, target);
  if (
    relative === ""
    || relative === ".."
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw new Error(`authority path escapes root: ${target}`);
  }
  const paths = [authority];
  let cursor = authority;
  for (const part of relative.split(path.sep).slice(0, -1)) {
    cursor = path.join(cursor, part);
    paths.push(cursor);
  }
  return paths.map((directoryPath) => {
    const metadata = fs.lstatSync(directoryPath, { bigint: true });
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`unsafe authority directory: ${directoryPath}`);
    }
    return { path: directoryPath, metadata };
  });
}

function assertAuthorityDirectories(directories, label) {
  for (const directory of directories) {
    const current = fs.lstatSync(directory.path, { bigint: true });
    if (!current.isDirectory() || !sameAuthorityIdentity(directory.metadata, current)) {
      throw new Error(`${label} directory changed while reading: ${directory.path}`);
    }
  }
}

function sameAuthorityIdentity(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.nlink === right.nlink
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function holdAuthorityReadForTest(pathName) {
  const target = process.env.AGENT_FLOW_TEST_HOLD_JSON_AUTH_PATH;
  if (target && path.resolve(target) !== path.resolve(pathName)) return;
  const milliseconds = Number.parseInt(
    process.env.AGENT_FLOW_TEST_HOLD_AFTER_JSON_AUTH_OPEN_MS || "0",
    10,
  );
  if (!Number.isInteger(milliseconds) || milliseconds <= 0 || milliseconds > 10_000) return;
  process.stderr.write("agent-flow:test-json-authority-opened\n");
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}
