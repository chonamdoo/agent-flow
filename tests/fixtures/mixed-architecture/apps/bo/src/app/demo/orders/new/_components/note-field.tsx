"use client";

import { useFormState } from "react-hook-form";
import { TextInput } from "@/ui/text-input";
import { notePath } from "../_lib/rhf-path";
import { validateNote } from "../_lib/schema";
import type { OrderFieldProps } from "../_types/form";

export function NoteField({ control, register }: OrderFieldProps) {
  const name = notePath(0);
  const { errors } = useFormState({ control, name, exact: true });
  const lineError = errors.lines?.[0];
  const noteError = lineError && "note" in lineError ? lineError.note : undefined;
  const message = noteError && typeof noteError === "object"
    && "message" in noteError && typeof noteError.message === "string"
    ? noteError.message : undefined;
  return (
    <div>
      <label htmlFor="order-note">Order note</label>
      <TextInput id="order-note" {...register(name, { validate: validateNote })}
        aria-invalid={Boolean(message)} aria-describedby="note-error" />
      <p id="note-error">{message}</p>
    </div>
  );
}
