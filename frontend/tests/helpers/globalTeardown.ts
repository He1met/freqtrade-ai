import { lstatSync, readFileSync, realpathSync, rmSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, resolve } from "node:path";

export default function globalTeardown(): void {
  const registryValue = process.env.E2E_ACCEPTANCE_REGISTRY;
  if (!registryValue) return;
  const registry = resolve(registryValue);
  const parent = realpathSync(process.env.E2E_TMP_PARENT ?? tmpdir());
  if (
    dirname(registry) !== parent ||
    !basename(registry).startsWith("freqtrade-ai-issue-433-registry-") ||
    !basename(registry).endsWith(".json")
  ) {
    throw new Error("refusing unsafe acceptance cleanup registry");
  }
  let manifest: {
    canonical_root: string;
    database: string;
    manifest_path: string;
    safety: Record<string, boolean>;
  };
  try {
    const metadata = lstatSync(registry);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error("acceptance cleanup registry is not a regular file");
    }
    manifest = JSON.parse(readFileSync(registry, "utf8"));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  const root = resolve(manifest.canonical_root);
  if (
    dirname(root) !== parent ||
    !basename(root).startsWith("freqtrade-ai-issue-433-") ||
    basename(root).startsWith("freqtrade-ai-issue-433-registry-")
  ) {
    throw new Error("refusing unowned acceptance cleanup root");
  }
  const rootMetadata = lstatSync(root);
  if (rootMetadata.isSymbolicLink() || !rootMetadata.isDirectory()) {
    throw new Error("refusing symlink acceptance cleanup root");
  }
  if (
    dirname(resolve(manifest.manifest_path)) !== root ||
    !resolve(manifest.database).startsWith(`${root}/`) ||
    Object.values(manifest.safety).some((value) => value !== false)
  ) {
    throw new Error("acceptance cleanup manifest failed ownership validation");
  }
  rmSync(root, { recursive: true });
  unlinkSync(registry);
}
