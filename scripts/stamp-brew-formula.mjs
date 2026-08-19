#!/usr/bin/env node
// 태그가 생긴 뒤에만 알 수 있는 두 값(tarball URL, sha256)을 formula에 박는다.
//
// 손으로 적지 않는 이유는 sha256이 태그 tarball을 받아야 나오는 값이기 때문이다.
// 사람이 옮겨 적는 자리에 두면 한 글자만 틀려도 `brew install`이 통째로 실패하고,
// 그 실패는 설치 시점에야 드러난다.
//
// usage: node scripts/stamp-brew-formula.mjs --version <x.y.z> --sha256 <hex>

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FORMULA = path.join(ROOT, "Formula", "agent-flow.rb");
const PYPROJECT = path.join(ROOT, "pyproject.toml");
const REPO = "chonamdoo/agent-flow";
const SHA256 = /^[0-9a-f]{64}$/;
const SEMVER = /^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$/;
// stable stanza는 homepage 다음, license 앞이다. Homebrew의 ComponentsOrder cop이
// 그 순서를 어떤 tap에서도 강제한다.
const HOMEPAGE_LINE = /^ {2}homepage ".*"$/m;
const STABLE_STANZA = /^ {2}url ".*"\n {2}sha256 ".*"\n/m;

function option(name) {
  const flag = `--${name}`;
  const index = process.argv.indexOf(flag);
  if (index === -1) return undefined;
  const value = process.argv[index + 1];
  if (value === undefined || value.startsWith("-")) {
    fail(`${flag} requires a value`);
  }
  return value;
}

function fail(message) {
  console.error(message);
  process.exit(1);
}

const version = option("version");
const sha256 = option("sha256");
if (!version || !sha256) {
  fail("usage: node scripts/stamp-brew-formula.mjs --version <x.y.z> --sha256 <hex>");
}
if (!SEMVER.test(version)) fail(`not a version: ${version}`);
if (!SHA256.test(sha256)) fail(`not a sha256 digest: ${sha256}`);

// 태그와 소스가 선언한 버전이 다르면 formula는 자기가 설치하는 트리와 다른 버전을
// 주장하게 된다. 그 상태를 릴리스로 내보내지 않는다.
const declared = fs
  .readFileSync(PYPROJECT, "utf8")
  .match(/^version = "(.+)"$/m)?.[1];
if (declared !== version) {
  fail(`pyproject.toml declares version ${declared}, the release is ${version}`);
}

const stanza =
  `  url "https://github.com/${REPO}/archive/refs/tags/v${version}.tar.gz"\n`
  + `  sha256 "${sha256}"\n`;
const original = fs.readFileSync(FORMULA, "utf8");
let updated;
if (STABLE_STANZA.test(original)) {
  updated = original.replace(STABLE_STANZA, stanza);
} else {
  const homepage = original.match(HOMEPAGE_LINE);
  if (!homepage) fail(`${FORMULA} has no homepage line to anchor the stable stanza`);
  updated = original.replace(HOMEPAGE_LINE, `${homepage[0]}\n${stanza.trimEnd()}`);
}
if (updated === original) {
  console.log(`Formula/agent-flow.rb already points at v${version}`);
  process.exit(0);
}
fs.writeFileSync(FORMULA, updated);
console.log(`Formula/agent-flow.rb now installs v${version} (${sha256})`);
