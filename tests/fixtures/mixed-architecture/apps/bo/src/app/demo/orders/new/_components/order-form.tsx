"use client";

import { useState } from "react";
import { FormProvider, useForm } from "react-hook-form";
import { orderSchema } from "../_lib/schema";
import type { OrderDraft } from "../_lib/schema";
import { toPayload } from "../_lib/transform";
import type { OrderPayload } from "../_lib/transform";
import { NoteField } from "./note-field";
import { ReferenceField } from "./reference-field";

export function OrderForm() {
  const methods = useForm<OrderDraft>({
    defaultValues: { reference: "", lines: [{ kind: "note", note: "" }] },
  });
  const [submitted, setSubmitted] = useState<OrderPayload | null>(null);
  const [validationMessage, setValidationMessage] = useState<string | null>(null);

  function submit(values: OrderDraft) {
    const result = orderSchema.safeParse(values);
    if (!result.success) {
      setValidationMessage(result.error.issues.map((issue) => issue.message).join(" "));
      return;
    }
    setValidationMessage(null);
    setSubmitted(toPayload(result.data));
  }

  return (
    <FormProvider {...methods}>
      <form onSubmit={methods.handleSubmit(submit)}>
        <ReferenceField control={methods.control} register={methods.register} />
        <NoteField control={methods.control} register={methods.register} />
        <button type="submit">Prepare payload</button>
        {validationMessage ? <p role="alert">{validationMessage}</p> : null}
      </form>
      {submitted ? (
        <section aria-label="Last submitted payload" aria-live="polite">
          <h2>Last submitted payload (local only)</h2>
          <pre>{JSON.stringify(submitted, null, 2)}</pre>
        </section>
      ) : null}
    </FormProvider>
  );
}
