// 설명 요약과 profile YAML에 쓰는 제한된 파서다. 설치 의존성과 아키텍처 메타데이터는
// Python `skill_metadata`가 해석한다.
//
// 지원 범위는 skill frontmatter가 실제로 쓰는 부분으로 한정한다: 단일 행 스칼라,
// 인라인/블록 리스트, block scalar(`>`, `|`)와 chomping 지시자(`-`, `+`).
// 명시적 들여쓰기 지시자(`>2`)와 more-indented 줄 보존은 다루지 않는다.

export function parseSimpleYaml(text, { strictKeys = [] } = {}) {
  const lines = text.split(/\r?\n/);
  // 마지막 개행이 만든 빈 원소는 내용이 아니다. 남겨 두면 block scalar의 keep(`+`)
  // 처리에서 존재하지 않는 빈 줄을 하나 더 세어 PyYAML보다 줄바꿈이 많아진다.
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  const required = new Set(strictKeys);
  const fail = (index, detail) => {
    throw new Error(`unsupported or invalid YAML at line ${index + 1}: ${detail}`);
  };
  const nextContent = (start) => {
    while (start < lines.length && /^\s*(?:#.*)?$/.test(lines[start])) start += 1;
    return start;
  };
  const indentation = (index) => lines[index].length - lines[index].trimStart().length;
  const parseBlock = (start, indent, strict, sequence = false) => {
    const result = sequence ? [] : {};
    let index = start;
    while ((index = nextContent(index)) < lines.length) {
      const currentIndent = indentation(index);
      if (currentIndent < indent) break;
      if (currentIndent !== indent || /^\s*\t/.test(lines[index])) {
        if (strict) fail(index, "unexpected indentation");
        index += 1;
        continue;
      }
      const content = lines[index].slice(indent);
      const item = sequence
        ? content.match(/^-(?:\s+(.*)|$)/)
        : content.match(/^([A-Za-z0-9_-]+|'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"):\s*(.*)$/);
      if (!item) {
        if (sequence && !/^-(?:\s|$)/.test(content)) break;
        const candidate = content.match(/^([A-Za-z0-9_-]+|'(?:[^']|'')*'|"(?:[^"\\]|\\.)*")(?=\s|:|$)/);
        if (strict || (indent === 0 && candidate && required.has(parseYamlValue(candidate[1], true)))) {
          fail(index, "expected a mapping or scalar list item");
        }
        index += 1;
        continue;
      }
      const key = sequence ? result.length : parseYamlValue(item[1], true);
      const fieldStrict = strict || (indent === 0 && required.has(key));
      if (!sequence && Object.hasOwn(result, key) && fieldStrict) fail(index, `duplicate key ${key}`);
      const raw = yamlTokens(sequence ? item[1] || "" : item[2], null, fieldStrict)[0].trim();
      const block = raw.match(/^([>|])([+-]?)$/);
      let value;
      let next = index + 1;
      if (block) {
        const body = [];
        while (next < lines.length && (lines[next].trim() === "" || indentation(next) > indent)) {
          body.push(lines[next]);
          next += 1;
        }
        value = renderBlockScalar(body, block[1], block[2]);
      } else if (raw === "") {
        next = nextContent(next);
        const childSequence = next < lines.length && /^\s*-(?:\s|$)/.test(lines[next]);
        if (next < lines.length && (indentation(next) > indent || (childSequence && indentation(next) === indent))) {
          [value, next] = parseBlock(next, indentation(next), fieldStrict, childSequence);
        } else {
          value = fieldStrict ? null : [];
        }
      } else {
        value = parseYamlValue(raw, fieldStrict);
        const following = nextContent(next);
        if (fieldStrict && following < lines.length && indentation(following) > indent) {
          fail(following, "unexpected continuation after a scalar or flow list");
        }
      }
      Object.defineProperty(result, key, { value, enumerable: true, configurable: true, writable: true });
      index = next;
    }
    return [result, index];
  };
  return parseBlock(0, 0, false)[0];
}

function yamlTokens(raw, delimiter, strict) {
  const tokens = [];
  let start = 0;
  let quote = null;
  for (let index = 0; index < raw.length; index += 1) {
    const character = raw[index];
    if (quote) {
      if (quote === '"' && character === "\\") index += 1;
      else if (character === quote) {
        if (quote === "'" && raw[index + 1] === "'") index += 1;
        else quote = null;
      }
    } else if ((character === "'" || character === '"') && (index === 0 || /[\s[{,:]/.test(raw[index - 1]))) {
      quote = character;
    } else if (character === "#" && (index === 0 || /\s/.test(raw[index - 1]))) {
      return [...tokens, raw.slice(start, index)];
    } else if (character === delimiter) {
      tokens.push(raw.slice(start, index));
      start = index + 1;
    }
  }
  if (quote && strict) throw new Error("unsupported or invalid YAML: unterminated quoted scalar");
  return [...tokens, raw.slice(start)];
}

function parseYamlValue(raw, strict) {
  if (raw.startsWith("[")) {
    if (!raw.endsWith("]")) {
      if (strict) throw new Error("unsupported or invalid YAML: expected a single-line flow list");
      return raw;
    }
    const body = raw.slice(1, -1).trim();
    if (!body) return [];
    const items = yamlTokens(body, ",", strict);
    if (!items[items.length - 1].trim()) items.pop();
    return items.map((item) => parseYamlValue(item.trim(), strict));
  }
  if (raw.startsWith('"')) {
    try {
      return JSON.parse(raw);
    } catch {
      if (strict) throw new Error("unsupported or invalid YAML: invalid double-quoted scalar");
      return stripQuotes(raw);
    }
  }
  if (raw.startsWith("'")) {
    if (/^'(?:[^']|'')*'$/.test(raw)) return raw.slice(1, -1).replace(/''/g, "'");
    if (strict) throw new Error("unsupported or invalid YAML: invalid single-quoted scalar");
    return stripQuotes(raw);
  }
  if (strict) {
    if (/^[{}&*!|>@`%?,\]]|^-(?:\s|$)/.test(raw) || /:\s|:$/.test(raw) || /[\[\]{}]/.test(raw)) {
      throw new Error("unsupported or invalid YAML: expected a scalar or scalar list");
    }
    if (/^(?:null|~)?$/i.test(raw)) return null;
    if (/^(?:true|yes|on)$/i.test(raw)) return true;
    if (/^(?:false|no|off)$/i.test(raw)) return false;
    if (/^[+-]?(?:(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?|0[xob][0-9a-f_]+|\.(?:inf|nan))$/i.test(raw)) {
      throw new Error("unsupported or invalid YAML: quote numeric dependency values");
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
      throw new Error("unsupported or invalid YAML: quote timestamp dependency values");
    }
  }
  return raw;
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

export function splitFrontmatter(text, { strict = false } = {}) {
  const lines = text.replace(/^\uFEFF+/, "").split(
    /(?<=[\n\v\f\x1c-\x1e\x85\u2028\u2029])|(?<=\r)(?!\n)/u,
  );
  const delimiter = /^[\p{White_Space}\x1c-\x1f]*---[\p{White_Space}\x1c-\x1f]*$/u;
  if (!delimiter.test(lines[0])) {
    return null;
  }
  for (let index = 1; index < lines.length; index += 1) {
    if (delimiter.test(lines[index])) {
      return lines.slice(1, index).join("").replace(
        /(?:\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029])$/u, "\n",
      );
    }
  }
  if (strict) {
    throw new Error("unterminated frontmatter");
  }
  return null;
}
