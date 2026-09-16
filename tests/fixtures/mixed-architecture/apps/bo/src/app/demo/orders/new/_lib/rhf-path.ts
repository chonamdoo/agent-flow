import type { FieldPath } from "react-hook-form";
import type { OrderDraft } from "./schema";

type NoteLine = Extract<OrderDraft["lines"][number], { kind: "note" }>;
type NoteLeaf = Exclude<keyof NoteLine, "kind">;
type NotePath = Extract<FieldPath<OrderDraft>, `lines.${number}.${NoteLeaf}`>;

export function notePath(index: number): NotePath {
  return `lines.${index}.note`;
}
