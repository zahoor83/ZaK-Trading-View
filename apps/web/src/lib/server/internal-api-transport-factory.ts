import "server-only";

import { createRequire } from "node:module";

import type { InternalServiceTransport } from "./internal-api-transport";

const MODULE_SPEC_PATTERN =
  /^(?:\.\/)?[A-Za-z0-9@._/-]+$/;

export class InternalServiceTransportFactoryError extends Error {
  constructor() {
    super("Production transport factory is unavailable.");
    this.name = "InternalServiceTransportFactoryError";
  }
}

function isValidModuleSpecifier(value: string): boolean {
  if (!MODULE_SPEC_PATTERN.test(value)) {
    return false;
  }

  if (
    value.startsWith("/") ||
    value.includes("\\") ||
    value.includes("//")
  ) {
    return false;
  }

  const normalizedPath = value.startsWith("./")
    ? value.slice(2)
    : value;

  const segments = normalizedPath.split("/");

  return (
    segments.length > 0 &&
    segments.every(
      (segment) =>
        segment.length > 0 &&
        segment !== "." &&
        segment !== "..",
    )
  );
}

function isTransport(value: unknown): value is InternalServiceTransport {
  return (
    typeof value === "object" &&
    value !== null &&
    "fetch" in value &&
    typeof value.fetch === "function"
  );
}

/**
 * Load a trusted CommonJS-compatible deployment factory.
 *
 * The specification must come from protected server-side deployment
 * configuration. It must never come from a request or browser input.
 */
export async function loadProductionInternalServiceTransport(
  specification: string | undefined,
): Promise<InternalServiceTransport> {
  if (typeof specification !== "string") {
    throw new InternalServiceTransportFactoryError();
  }

  const separator = specification.indexOf(":");

  if (
    separator <= 0 ||
    separator !== specification.lastIndexOf(":")
  ) {
    throw new InternalServiceTransportFactoryError();
  }

  const moduleSpecifier = specification.slice(0, separator);
  const functionName = specification.slice(separator + 1);

  if (
    !isValidModuleSpecifier(moduleSpecifier) ||
    !/^[A-Za-z_][A-Za-z0-9_]*$/.test(functionName)
  ) {
    throw new InternalServiceTransportFactoryError();
  }

  try {
    // Resolve package and relative module specifiers from the web app root.
    const requireFromWebRoot = createRequire(
      `${process.cwd()}/package.json`,
    );

    const loadedModule: unknown = requireFromWebRoot(moduleSpecifier);

    if (
      typeof loadedModule !== "object" ||
      loadedModule === null ||
      !(functionName in loadedModule)
    ) {
      throw new Error("Factory export unavailable.");
    }

    const factory = (
      loadedModule as Record<string, unknown>
    )[functionName];

    if (typeof factory !== "function") {
      throw new Error("Factory export is not callable.");
    }

    const result: unknown = await (
      factory as () => unknown | Promise<unknown>
    )();

    if (!isTransport(result)) {
      throw new Error("Factory returned an invalid transport.");
    }

    return result;
  } catch {
    throw new InternalServiceTransportFactoryError();
  }
}

