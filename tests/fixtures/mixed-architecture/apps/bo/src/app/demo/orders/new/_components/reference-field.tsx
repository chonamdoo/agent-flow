"use client";

import { useFormState, useWatch } from "react-hook-form";
import { TextInput } from "@/ui/text-input";
import { validateReference } from "../_lib/schema";
import type { OrderFieldProps } from "../_types/form";

export function ReferenceField({ control, register }: OrderFieldProps) {
  const reference = useWatch({ control, name: "reference", exact: true });
  const { errors } = useFormState({ control, name: "reference", exact: true });
  return (
    <div>
      <label htmlFor="order-reference">Order reference</label>
      <TextInput id="order-reference" {...register("reference", { validate: validateReference })}
        aria-invalid={Boolean(errors.reference)} aria-describedby="reference-help" />
      <p id="reference-help">{errors.reference?.message ?? `Draft reference: ${reference || "empty"}`}</p>
    </div>
  );
}
