import fs from "node:fs";
import path from "node:path";

import {
  pathHasSymlink,
  pruneRetiredHooks,
  removeOmpHooksExtension,
  writeAtomicTextFile,
} from "./installer-shared.mjs";
import { publishManagedProject } from "./shared-hook-runtime.mjs";

const PROJECT_HOOK_REGISTRATIONS = Object.freeze([
  [".claude", "settings.json"],
  [".Codex", "hooks.json"],
  [".codex", "hooks.json"],
]);
const PROJECT_OMP_EXTENSION = Object.freeze([
  ".omp",
  "extensions",
  "agent-flow-hooks.ts",
]);

function pathState(target) {
  let identity;
  try {
    identity = fs.lstatSync(target);
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { kind: "absent" };
    }
    throw error;
  }
  if (identity.isSymbolicLink()) {
    return { kind: "symlink" };
  }
  if (!identity.isFile()) {
    return { kind: "other" };
  }
  return {
    kind: "file",
    content: fs.readFileSync(target, "utf8"),
    mode: identity.mode & 0o777,
  };
}

function sameState(left, right) {
  if (left.kind !== right.kind) {
    return false;
  }
  if (left.kind !== "file") {
    return true;
  }
  return left.mode === right.mode && left.content === right.content;
}

function snapshotPath(target) {
  const before = pathState(target);
  return { target, before, after: before };
}

function projectHookRegistrationSnapshots(root) {
  const seen = new Set();
  const registrations = [];
  for (const relative of PROJECT_HOOK_REGISTRATIONS) {
    const target = path.join(root, ...relative);
    let identity;
    try {
      identity = fs.realpathSync.native(target);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
      identity = path.resolve(target);
    }
    if (seen.has(identity)) {
      continue;
    }
    seen.add(identity);
    registrations.push({ relative, snapshot: snapshotPath(target) });
  }
  return registrations;
}

function restoreSnapshot(snapshot) {
  const current = pathState(snapshot.target);
  if (sameState(current, snapshot.before)) {
    return;
  }
  if (!sameState(current, snapshot.after)) {
    throw new Error(`project hook registration changed during rollback: ${snapshot.target}`);
  }
  if (snapshot.before.kind === "absent") {
    fs.unlinkSync(snapshot.target);
    return;
  }
  if (snapshot.before.kind !== "file") {
    throw new Error(`cannot restore non-regular project hook registration: ${snapshot.target}`);
  }
  writeAtomicTextFile(
    snapshot.target,
    snapshot.before.content,
    snapshot.before.mode,
  );
}

function restoreSnapshots(snapshots) {
  const errors = [];
  for (const snapshot of [...snapshots].reverse()) {
    try {
      restoreSnapshot(snapshot);
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }
  if (errors.length > 0) {
    throw new Error(errors.join("; "));
  }
}

function plannedJsonRegistration(root, snapshot) {
  if (snapshot.before.kind === "absent") {
    return null;
  }
  if (pathHasSymlink(root, snapshot.target)) {
    throw new Error(`refusing symlinked project hook registration: ${snapshot.target}`);
  }
  if (snapshot.before.kind !== "file") {
    throw new Error(`project hook registration is not a regular file: ${snapshot.target}`);
  }
  let settings;
  try {
    settings = JSON.parse(snapshot.before.content);
  } catch {
    throw new Error(`cannot safely retire project-local hook registration: ${snapshot.target}`);
  }
  if (!pruneRetiredHooks(settings, true, true, root)) {
    return null;
  }
  return `${JSON.stringify(settings, null, 2)}\n`;
}

function preflightProjectHookRegistrations(root, registrations, ompSnapshot) {
  for (const { snapshot } of registrations) {
    plannedJsonRegistration(root, snapshot);
  }
  if (pathHasSymlink(root, ompSnapshot.target)) {
    throw new Error(`refusing symlinked project hook registration: ${ompSnapshot.target}`);
  }
}

function stageJsonRegistration(root, relative, snapshot) {
  const content = plannedJsonRegistration(root, snapshot);
  if (content === null) {
    return;
  }
  const current = pathState(snapshot.target);
  if (!sameState(current, snapshot.before)) {
    throw new Error(`project hook registration changed before cutover: ${snapshot.target}`);
  }
  snapshot.after = { kind: "file", content, mode: 0o600 };
  writeAtomicTextFile(snapshot.target, content);
  console.log(`  - removed project-local hook registration: ${path.join(...relative)}`);
}

function stageProjectHookRegistrations(root, registrations, ompSnapshot) {
  for (const { relative, snapshot } of registrations) {
    stageJsonRegistration(root, relative, snapshot);
  }
  try {
    removeOmpHooksExtension(root);
  } finally {
    ompSnapshot.after = pathState(ompSnapshot.target);
  }
}


function combinedError(error, detail) {
  const message = error instanceof Error ? error.message : String(error);
  return new Error(`${message}; ${detail}`);
}

export function publishManagedProjectCutover({
  root,
  manifest,
  retireLegacyRuntime,
}) {
  if (typeof retireLegacyRuntime !== "function") {
    throw new TypeError("retireLegacyRuntime must be a function");
  }
  const registrations = projectHookRegistrationSnapshots(root);
  const ompSnapshot = snapshotPath(path.join(root, ...PROJECT_OMP_EXTENSION));
  const snapshots = [
    ...registrations.map(({ snapshot }) => snapshot),
    ompSnapshot,
  ];
  preflightProjectHookRegistrations(root, registrations, ompSnapshot);
  const publication = publishManagedProject({
    root,
    manifest,
    deferCommit: true,
  });
  return publication.commit(() => {
    let retirement = null;
    try {
      stageProjectHookRegistrations(root, registrations, ompSnapshot);
      retirement = retireLegacyRuntime();
    } catch (error) {
      try {
        restoreSnapshots(snapshots);
      } catch (rollbackError) {
        throw combinedError(
          error,
          `project hook registration rollback failed: ${
            rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
          }`,
        );
      }
      throw error;
    }
    return {
      commit() {
        retirement.commit();
      },
      rollback() {
        const rollbackErrors = [];
        try {
          retirement.rollback();
        } catch (rollbackError) {
          rollbackErrors.push(
            `legacy runtime rollback failed: ${
              rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
            }`,
          );
        }
        try {
          restoreSnapshots(snapshots);
        } catch (rollbackError) {
          rollbackErrors.push(
            `project hook registration rollback failed: ${
              rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
            }`,
          );
        }
        if (rollbackErrors.length > 0) {
          throw new Error(rollbackErrors.join("; "));
        }
      },
    };
  });
}
