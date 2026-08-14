export const LAB_ACCEPTANCE_PROFILES = Object.freeze([
  Object.freeze({
    name: "complete-current",
    backendPort: 41_001,
    frontendPort: 42_001,
  }),
  Object.freeze({ name: "empty", backendPort: 41_002, frontendPort: 42_002 }),
  Object.freeze({ name: "missing-result", backendPort: 41_003, frontendPort: 42_003 }),
  Object.freeze({ name: "missing-strategy", backendPort: 41_004, frontendPort: 42_004 }),
  Object.freeze({ name: "long-evidence", backendPort: 41_005, frontendPort: 42_005 }),
]);

export function acceptanceRepeatCount(environment = process.env) {
  return environment.CI === "true" ? 2 : 1;
}

export function validateAcceptanceProfiles(profiles = LAB_ACCEPTANCE_PROFILES) {
  const names = new Set();
  const ports = new Set();
  for (const profile of profiles) {
    if (!profile.name || names.has(profile.name)) {
      throw new Error("acceptance profile names must be non-empty and unique");
    }
    names.add(profile.name);
    for (const port of [profile.backendPort, profile.frontendPort]) {
      if (!Number.isInteger(port) || port < 40_001 || port > 49_999 || ports.has(port)) {
        throw new Error("acceptance ports must be unique integers in the isolated band");
      }
      ports.add(port);
    }
  }
  return profiles;
}
