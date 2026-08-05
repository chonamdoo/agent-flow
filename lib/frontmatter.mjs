// skill frontmatter 파서의 정본.
//
// 같은 파서가 진입점마다 한 벌씩 있으면 index 기록과 설치 선택이 서로 다른 값을
// 본다. 실제로 `description: >`가 `">"` 한 글자로 index에 실렸고, PyYAML을 쓰는
// Python 런타임만 올바른 값을 봤다.
//
// 지원 범위는 skill frontmatter가 실제로 쓰는 부분으로 한정한다: 단일 행 스칼라,
// 인라인/블록 리스트, block scalar(`>`, `|`)와 chomping 지시자(`-`, `+`).
// 명시적 들여쓰기 지시자(`>2`)와 more-indented 줄 보존은 다루지 않는다.

export function parseSimpleYaml(text) {
  const metadata = {};
  const lines = text.split(/\r?\n/);
  // 마지막 개행이 만든 빈 원소는 내용이 아니다. 남겨 두면 block scalar의 keep(`+`)
  // 처리에서 존재하지 않는 빈 줄을 하나 더 세어 PyYAML보다 줄바꿈이 많아진다.
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  let listKey = null;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const listItem = line.match(/^\s+-\s*(.+)$/);
    if (listItem && listKey) {
      metadata[listKey].push(stripQuotes(listItem[1].trim()));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) {
      listKey = null;
      continue;
    }
    const key = match[1];
    const raw = match[2].trim();
    const block = raw.match(/^([>|])([+-]?)$/);
    if (block) {
      const body = [];
      while (index + 1 < lines.length) {
        const next = lines[index + 1];
        if (next.trim() !== "" && !/^\s/.test(next)) {
          break;
        }
        body.push(next);
        index += 1;
      }
      metadata[key] = renderBlockScalar(body, block[1], block[2]);
      listKey = null;
      continue;
    }
    if (raw.startsWith("[") && raw.endsWith("]")) {
      metadata[key] = raw
        .slice(1, -1)
        .split(",")
        .map((item) => stripQuotes(item.trim()))
        .filter(Boolean);
      listKey = null;
    } else if (raw === "") {
      metadata[key] = [];
      listKey = key;
    } else {
      metadata[key] = stripQuotes(raw);
      listKey = null;
    }
  }
  return metadata;
}

function renderBlockScalar(rawLines, style, chomping) {
  const indent = blockIndent(rawLines);
  const lines = rawLines.map((line) => (line.length >= indent ? line.slice(indent) : line.trim()));
  let trailing = 0;
  while (lines.length > 0 && lines[lines.length - 1].trim() === "") {
    lines.pop();
    trailing += 1;
  }
  if (lines.length === 0) {
    return chomping === "+" ? "\n".repeat(trailing) : "";
  }
  const body = style === "|" ? lines.join("\n") : foldLines(lines);
  if (chomping === "-") {
    return body;
  }
  // clip은 줄바꿈 하나, keep(`+`)은 뒤따르던 빈 줄까지 남긴다. PyYAML과 같은 규칙이다.
  return chomping === "+" ? `${body}\n${"\n".repeat(trailing)}` : `${body}\n`;
}

function blockIndent(lines) {
  for (const line of lines) {
    if (line.trim() !== "") {
      return line.length - line.trimStart().length;
    }
  }
  return 0;
}

function foldLines(lines) {
  // 접기 규칙은 PyYAML과 같아야 한다.
  // - 빈 줄은 줄바꿈이 되고, 줄바꿈 **뒤에 오는** 줄은 공백 없이 이어 붙는다.
  //   공백을 넣으면 `"a\nb\n"`가 `"a\n b\n"`이 된다.
  // - 블록 들여쓰기보다 더 들여쓴 줄은 접지 않고 그대로 둔다. 앞뒤로 줄바꿈이 붙는다.
  let folded = "";
  let afterBreak = true;
  let literalRun = false;
  for (const line of lines) {
    if (line.trim() === "") {
      folded += "\n";
      afterBreak = true;
      literalRun = false;
      continue;
    }
    if (/^\s/.test(line)) {
      // 연속한 more-indented 줄 사이에는 줄바꿈을 더 넣지 않는다. 앞 줄이 이미
      // 자기 줄바꿈을 남겼으므로 한 번 더 넣으면 빈 줄이 생긴다.
      folded += folded === "" || literalRun ? line : `\n${line}`;
      folded += "\n";
      afterBreak = true;
      literalRun = true;
      continue;
    }
    folded += afterBreak ? line : ` ${line}`;
    afterBreak = false;
    literalRun = false;
  }
  return folded.endsWith("\n") ? folded.slice(0, -1) : folded;
}

function stripQuotes(value) {
  return value.replace(/^['"]|['"]$/g, "");
}

// Python `skill_resolver.skill_summary`와 같은 규칙이어야 한다. 규칙이 갈리면 같은
// skill이 AGENTS.md 인덱스와 phase 프롬프트에서 다르게 보인다.
const SUMMARY_MAX_CHARS = 140;

export function skillSummaryFromMarkdown(text) {
  const frontmatter = splitFrontmatter(text);
  if (frontmatter === null) {
    return "";
  }
  const description = parseSimpleYaml(frontmatter).description;
  if (typeof description !== "string") {
    return "";
  }
  const collapsed = description.split(/\s+/).filter(Boolean).join(" ");
  if (collapsed === "") {
    return "";
  }
  const head = collapsed.split(/(?<=[.!?])\s/, 1)[0];
  if (head.length <= SUMMARY_MAX_CHARS) {
    return head;
  }
  return `${head.slice(0, SUMMARY_MAX_CHARS - 1).trimEnd()}\u2026`;
}

export function splitFrontmatter(text) {
  // CRLF로 저장된 SKILL.md도 같은 값을 내야 한다. LF만 받으면 그 파일은 JS에서
  // summary가 비고 PyYAML을 쓰는 Python에서는 나와서, 두 언어가 다시 갈린다.
  const match = text.match(/^---\r?\n/);
  if (match === null) {
    return null;
  }
  const start = match[0].length;
  const end = text.slice(start).search(/\r?\n---/);
  return end === -1 ? null : `${text.slice(start, start + end)}\n`;
}
