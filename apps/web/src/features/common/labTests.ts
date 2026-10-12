import type { TFunction } from "i18next";

/** A lab test's name in the reader's language when it is one of the standard tests. */
export function testLabel(t: TFunction, test: Record<string, unknown>): string {
  const name = String(test.name ?? test.code ?? "");
  return t(`lab.tests.${String(test.code ?? "")}`, { defaultValue: name });
}
