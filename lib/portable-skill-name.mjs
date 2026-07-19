export function compareCodePoints(left, right) {
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

export function isPortableSkillName(value) {
  if (typeof value !== "string") return false;
  const match = /^[A-Za-z0-9._-]+$/.exec(value);
  return match !== null
    && match[0] === value
    && !value.startsWith(".")
    && !value.includes("..");
}

export function portableSkillCasefold(value) {
  if (!isPortableSkillName(value)) {
    throw new Error(`unsafe skill name: ${value}`);
  }
  return value.toLowerCase();
}
