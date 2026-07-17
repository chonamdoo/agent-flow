import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";


export function runtimeParityFailures(sourceRoot, installRoot, label = "current installed runtime") {
  const failures = [];
  const copies = [
    ["workflows", path.join(".agent-flow", "workflows")],
    ["profiles", path.join(".agent-flow", "profiles")],
    [path.join("src", "agent_flow"), path.join(".agent-flow", "runtime", "python", "agent_flow")],
    ...[
      "bin",
      "lib",
      "workflows",
      "profiles",
      "skills",
      "templates",
      "scripts",
      "bootstrap",
      path.join("src", "agent_flow"),
      path.join(".Codex", "agents"),
      path.join(".Codex", "rules"),
      path.join(".Codex", "context"),
      path.join(".claude", "agents"),
    ].map((source) => [source, path.join(".agent-flow", "runtime", "node", source)]),
  ];
  for (const [source, target] of copies) {
    const canonicalModes = target.startsWith(`${path.join(".agent-flow", "runtime")}${path.sep}`);
    assertExactDirectoryCopy(
      failures,
      `${label} ${target}`,
      path.join(sourceRoot, source),
      path.join(installRoot, target),
      canonicalModes,
    );
  }
  for (const runtime of ["node", "python"]) {
    const root = path.join(".agent-flow", "runtime", runtime);
    assertExactRuntimeRootLayout(
      failures,
      `${label} ${runtime === "node" ? "Node" : "Python"}`,
      path.join(installRoot, root),
      copies
        .filter(([_source, target]) => target.startsWith(`${root}${path.sep}`))
        .map(([_source, target]) => path.relative(root, target)),
    );
  }
  return failures;
}


function exactTreeEntries(
  root,
  { ignorePythonCaches = false, canonicalDirectoryMode = false, ignoreModes = false } = {},
) {
  const entries = new Map();
  const visit = (current, relative) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      if (ignorePythonCaches && (entry.name === "__pycache__" || entry.name.endsWith(".pyc"))) continue;
      const entryPath = path.join(current, entry.name);
      const entryRelative = relative ? path.join(relative, entry.name) : entry.name;
      const mode = fs.lstatSync(entryPath).mode & 0o777;
      if (entry.isDirectory()) {
        entries.set(
          entryRelative,
          `directory:${ignoreModes ? "*" : (canonicalDirectoryMode ? 0o755 : mode).toString(8)}`,
        );
        visit(entryPath, entryRelative);
      } else if (entry.isFile()) {
        entries.set(
          entryRelative,
          `file:${ignoreModes ? "*" : mode.toString(8)}:${createHash("sha256").update(fs.readFileSync(entryPath)).digest("hex")}`,
        );
      } else if (entry.isSymbolicLink()) {
        entries.set(entryRelative, `symlink:${fs.readlinkSync(entryPath)}`);
      } else {
        entries.set(entryRelative, "unsupported");
      }
    }
  };
  visit(root, "");
  return entries;
}


function assertExactRuntimeRootLayout(failures, label, runtimeRoot, copiedDestinations) {
  if (!fs.existsSync(runtimeRoot)) {
    failures.push(`${label} runtime root is missing`);
    return;
  }
  const metadata = fs.lstatSync(runtimeRoot);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    failures.push(`${label} runtime root type or mode differs`);
    return;
  }
  if ((metadata.mode & 0o777) !== 0o755) failures.push(`${label} runtime root type or mode differs`);
  for (const name of exactTreeEntries(runtimeRoot).keys()) {
    const covered = copiedDestinations.some((destination) => (
      name === destination
      || name.startsWith(`${destination}${path.sep}`)
      || destination.startsWith(`${name}${path.sep}`)
    ));
    if (!covered) failures.push(`${label} unexpected runtime entry ${name}`);
  }
}


function assertExactDirectoryCopy(failures, label, source, target, canonicalModes = true) {
  if (!fs.existsSync(source) || !fs.existsSync(target)) {
    failures.push(`${label} exact copy path is missing`);
    return;
  }
  const sourceRoot = fs.lstatSync(source);
  const targetRoot = fs.lstatSync(target);
  if (
    sourceRoot.isSymbolicLink()
    || targetRoot.isSymbolicLink()
    || !sourceRoot.isDirectory()
    || !targetRoot.isDirectory()
    || (canonicalModes && (targetRoot.mode & 0o777) !== 0o755)
  ) failures.push(`${label} root type or mode differs`);
  const sourceEntries = exactTreeEntries(source, {
    ignorePythonCaches: true,
    canonicalDirectoryMode: canonicalModes,
    ignoreModes: !canonicalModes,
  });
  const targetEntries = exactTreeEntries(target, { ignoreModes: !canonicalModes });
  const names = [...new Set([...sourceEntries.keys(), ...targetEntries.keys()])].sort();
  for (const name of names) {
    if (sourceEntries.get(name) !== targetEntries.get(name)) failures.push(`${label} differs at ${name}`);
  }
}
