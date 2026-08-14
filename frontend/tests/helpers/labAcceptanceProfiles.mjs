import { randomInt } from "node:crypto";

const PORT_MIN = 50_000;
const PORT_MAX_EXCLUSIVE = 60_000;

export const LAB_ACCEPTANCE_PROFILES = Object.freeze([
  Object.freeze({ name: "complete-current" }),
  Object.freeze({ name: "empty" }),
  Object.freeze({ name: "missing-result" }),
  Object.freeze({ name: "missing-strategy" }),
  Object.freeze({ name: "long-evidence" }),
]);

export function acceptanceRepeatCount(environment = process.env) {
  return environment.CI === "true" ? 2 : 1;
}

export function validateAcceptanceProfiles(profiles = LAB_ACCEPTANCE_PROFILES) {
  const names = new Set();
  for (const profile of profiles) {
    if (!profile.name || names.has(profile.name)) {
      throw new Error("acceptance profile names must be non-empty and unique");
    }
    names.add(profile.name);
  }
  return profiles;
}

export async function allocateIsolatedPort({
  usedPorts,
  isAvailable,
  start = randomInt(PORT_MIN, PORT_MAX_EXCLUSIVE),
}) {
  if (!(usedPorts instanceof Set) || typeof isAvailable !== "function") {
    throw new Error("isolated port allocator requires a Set and availability probe");
  }
  if (!Number.isInteger(start) || start < PORT_MIN || start >= PORT_MAX_EXCLUSIVE) {
    throw new Error("isolated port allocation start is outside the safe band");
  }
  const span = PORT_MAX_EXCLUSIVE - PORT_MIN;
  for (let offset = 0; offset < span; offset += 1) {
    const candidate = PORT_MIN + ((start - PORT_MIN + offset) % span);
    if (!usedPorts.has(candidate) && (await isAvailable(candidate))) {
      usedPorts.add(candidate);
      return candidate;
    }
  }
  throw new Error("no bindable isolated acceptance port is available");
}
