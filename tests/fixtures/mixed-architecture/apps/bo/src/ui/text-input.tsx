"use client";

import type { ComponentPropsWithRef } from "react";

export function TextInput(props: ComponentPropsWithRef<"input">) {
  return <input {...props} />;
}
