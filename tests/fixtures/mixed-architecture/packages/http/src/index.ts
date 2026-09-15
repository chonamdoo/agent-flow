export function parseResponse<T>(body: string, decode: (value: unknown) => T): T {
  const value: unknown = JSON.parse(body);
  return decode(value);
}
