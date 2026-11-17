/** Turns a snake_case key into camelCase. */
type CamelKey<S extends string> = S extends `${infer Head}_${infer Tail}` ? `${Head}${Capitalize<CamelKey<Tail>>}` : S;

/** A socket API type with every key in camelCase, as the SDK returns it. */
export type Camelize<T> = T extends readonly (infer Item)[]
  ? Camelize<Item>[]
  : T extends object
    ? { [Key in keyof T as Key extends string ? CamelKey<Key> : Key]: Camelize<T[Key]> }
    : T;

/** Converts every key of a decoded JSON answer from snake_case to camelCase. */
export function camelize<T>(value: T): Camelize<T> {
  if (Array.isArray(value)) {
    return value.map((item) => camelize(item)) as Camelize<T>;
  }
  if (value !== null && typeof value === "object") {
    const converted: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      converted[key.replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase())] = camelize(item);
    }
    return converted as Camelize<T>;
  }
  return value as Camelize<T>;
}
