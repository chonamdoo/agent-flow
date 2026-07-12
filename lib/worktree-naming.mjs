import crypto from "node:crypto";

const ARTIFACT_EXTENSION_SOURCE = "fig|figma|png|jpe?g|gif|webp|svg|pdf|sketch|avif|bmp|ico|tiff?|heic|heif|psd";
const SEMANTIC_NEUTRAL_FALLBACK = "task-attachment";
const MARKDOWN_REFERENCE_PATTERN = /(!?)\[([^\]\r\n]*)\]\(((?:[^()\r\n]|\([^()\r\n]*\))*)\)/gu;
const URL_PATTERN = /(?:https?:\/\/|file:\/\/|www\.)[^\s<]+|figma\.com\/[^\s<]+/giu;
const QUOTED_ARTIFACT_PATTERN = new RegExp(
  `(["'\\x60“‘])[^\\r\\n]*?\\.(?:${ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\\s"'\\x60”’]*)?["'\\x60”’]`,
  "giu",
);
const PATH_ARTIFACT_PATTERN = new RegExp(
  `(^|[\\s(\\[\\{,:;])(?:[A-Za-z]:[\\\\/]|(?:~|\\.\\.?)[\\\\/]|[\\\\/]|(?:[^\\s/\\\\()<>\\[\\]{}:;,]+[\\\\/])+)[^\\r\\n]*?\\.(?:${ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\\s:;,)\\]\\}>]*)?(?=$|[\\s:;,)\\]\\}>])`,
  "giu",
);
const BARE_ARTIFACT_PATTERN = new RegExp(
  `(^|[\\s(\\[\\{,:;])[^\\s/\\\\()<>\\[\\]{}]+?\\.(?:${ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\\s:;,)\\]\\}>]*)?(?=$|[\\s:;,)\\]\\}>])`,
  "giu",
);
const ARTIFACT_DESTINATION_PATTERN = new RegExp(
  `\\.(?:${ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\\s>]*)?(?:\\s+["'][^"']*["'])?\\s*>?$`,
  "iu",
);
const FIGMA_DESTINATION_PATTERN = /^(?:https?:\/\/)?(?:www\.)?figma\.com\//iu;
const SLUG_CHARACTER_PATTERN = /[\p{L}\p{N}]/u;

function markdownReplacement(_match, imageMarker, label, destination) {
  const normalizedDestination = destination.trim().replace(/^<|>$/g, "");
  if (
    imageMarker
    || FIGMA_DESTINATION_PATTERN.test(normalizedDestination)
    || ARTIFACT_DESTINATION_PATTERN.test(normalizedDestination)
  ) {
    return " ";
  }
  return ` ${label} `;
}

function stripArtifactReferences(value) {
  return value
    .replace(MARKDOWN_REFERENCE_PATTERN, markdownReplacement)
    .replace(URL_PATTERN, " ")
    .replace(QUOTED_ARTIFACT_PATTERN, " ")
    .replace(PATH_ARTIFACT_PATTERN, (_match, prefix) => prefix)
    .replace(BARE_ARTIFACT_PATTERN, (_match, prefix) => prefix);
}

export function semanticWorktreeSlug(task, maxLength = 60) {
  let normalized = String(task).normalize("NFKC").trim().toLowerCase();
  if (normalized.startsWith("feat-")) normalized = normalized.slice(5);

  const stripped = stripArtifactReferences(normalized);
  let slug = Array.from(stripped, (character) => (
    SLUG_CHARACTER_PATTERN.test(character) ? character : "-"
  ))
    .join("")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");

  if (!slug) {
    slug = SEMANTIC_NEUTRAL_FALLBACK;
  }

  const limit = Math.max(12, Math.min(Number.parseInt(maxLength, 10) || 60, 100));
  const codePoints = Array.from(slug);
  if (codePoints.length > limit) {
    const digest = crypto.createHash("sha1").update(slug).digest("hex").slice(0, 6);
    const prefix = codePoints.slice(0, limit - 7).join("").replace(/-+$/g, "");
    slug = `${prefix}-${digest}`;
  }
  return slug;
}
