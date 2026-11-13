// Registers validators once, at startup.
export const registry = new Map<string, () => boolean>();

/** Adds a validator under its name. */
export function register(name: string, check: () => boolean): void {
  registry.set(name, check);
}
