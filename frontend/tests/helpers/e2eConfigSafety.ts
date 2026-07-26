import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute } from "node:path";

export const UNSAFE_SHELL = /[\r\n\0`$;&|<>(){}[\]*?!'"\\]/;

export function safeAbsoluteDirectory(name: string, value: string): string {
  if (!isAbsolute(value) || UNSAFE_SHELL.test(value)) {
    throw new Error(`${name} must be an absolute shell-safe path.`);
  }
  const metadata = lstatSync(value);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`${name} must be a real directory, not a symlink.`);
  }
  return realpathSync(value);
}

export function safePythonBinary(value: string | undefined): string {
  if (value === undefined) return "python3";
  if (!isAbsolute(value) || UNSAFE_SHELL.test(value)) {
    throw new Error("PYTHON_BIN must be an absolute shell-safe executable path.");
  }
  const metadata = lstatSync(value);
  if (!metadata.isFile() && !metadata.isSymbolicLink()) {
    throw new Error("PYTHON_BIN must resolve to an executable file.");
  }
  const resolved = realpathSync(value);
  if (!lstatSync(resolved).isFile()) {
    throw new Error("PYTHON_BIN must resolve to an executable file.");
  }
  return value;
}
