import { test, expect } from "@playwright/test";

import {
  InternalServiceTransportFactoryError,
  loadProductionInternalServiceTransport,
} from "../../src/lib/server/internal-api-transport-factory";

const fixture =
  "./tests/transport/production-transport-fixture.cjs";

test("loads a valid production transport factory", async () => {
  const transport = await loadProductionInternalServiceTransport(
    `${fixture}:buildTransport`,
  );

  expect(typeof transport.fetch).toBe("function");

  const response = await transport.fetch(
    new URL("http://127.0.0.1:8000/internal/test"),
    { method: "GET" },
  );

  expect(response.status).toBe(200);
  expect(await response.text()).toBe("factory-transport");
});

test("rejects missing and malformed factory specifications", async () => {
  const invalidSpecifications = [
    undefined,
    "",
    "missing-colon",
    ":buildTransport",
    `${fixture}:`,
    `${fixture}:buildTransport:extra`,
    "../outside:buildTransport",
    "/absolute/path:buildTransport",
    `${fixture}:not-a-function-name`,
  ];

  for (const specification of invalidSpecifications) {
    await expect(
      loadProductionInternalServiceTransport(specification),
    ).rejects.toBeInstanceOf(InternalServiceTransportFactoryError);
  }
});

test("rejects a module that cannot be loaded", async () => {
  await expect(
    loadProductionInternalServiceTransport(
      "module_that_does_not_exist_for_zak:build",
    ),
  ).rejects.toBeInstanceOf(InternalServiceTransportFactoryError);
});

test("rejects a missing factory export", async () => {
  await expect(
    loadProductionInternalServiceTransport(
      `${fixture}:missingFactory`,
    ),
  ).rejects.toBeInstanceOf(InternalServiceTransportFactoryError);
});

test("rejects a factory that throws without exposing its error", async () => {
  let capturedError: unknown;

  try {
    await loadProductionInternalServiceTransport(
      `${fixture}:throwSensitiveError`,
    );
  } catch (error) {
    capturedError = error;
  }

  expect(capturedError).toBeInstanceOf(
    InternalServiceTransportFactoryError,
  );
  expect(String(capturedError)).not.toContain(
    "fixture-sensitive-secret-detail",
  );
});

test("rejects a factory that returns an invalid transport", async () => {
  await expect(
    loadProductionInternalServiceTransport(
      `${fixture}:buildInvalidTransport`,
    ),
  ).rejects.toBeInstanceOf(InternalServiceTransportFactoryError);
});
