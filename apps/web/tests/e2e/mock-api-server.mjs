import { createServer } from "node:http";
import {
  createHash,
  randomBytes,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";

const HOST = "127.0.0.1";
const PORT = 8000;
const CALLBACK_URL = "http://localhost:3000/api/auth/oidc/callback";

const transactions = new Map();
const sessions = new Map();

let failRevoke = false;
let sessionValidationMode = "valid";
let redirectInternal = false;
let oidcTransactionResponseMode = "valid";
let redirectTargetHits = 0;
let marketDataMode = "valid";
let paperSpotMode = "valid";
const paperSpotCaptures = [];
let paperFuturesMode = "valid";
const paperFuturesCaptures = [];

const paperManualTopUpMode = { initial: "valid" };
const paperManualTopUpCaptures = [];
let paperBootstrapMode = "valid";
const paperBootstrapCaptures = [];
let paperSpotCashAvailable = "1000.00";
let paperFuturesCashAvailable = "1000.00";
let paperSpotPortfolioStatus = "active";
let paperFuturesPortfolioStatus = "active";

const paperResetInitialSessions = {
  spot: {
    id: "33333333-3333-4333-8333-333333333333",
    sessionNumber: 1,
    bookId: "44444444-4444-4444-8444-444444444444",
  },
  futures: {
    id: "66666666-6666-4666-8666-666666666666",
    sessionNumber: 1,
    bookId: "77777777-7777-4777-8777-777777777777",
  },
};

let paperSpotSession = { ...paperResetInitialSessions.spot };
let paperFuturesSession = { ...paperResetInitialSessions.futures };

const paperResetMode = { initial: "valid" };
const paperResetCaptures = [];
const paperResetReplays = new Map();

function resetPaperResetFixtures() {
  paperSpotSession = { ...paperResetInitialSessions.spot };
  paperFuturesSession = { ...paperResetInitialSessions.futures };
  paperResetMode.initial = "valid";
  paperResetCaptures.splice(0);
  paperResetReplays.clear();
  paperSpotCashAvailable = "1000.00";
  paperFuturesCashAvailable = "1000.00";
  paperSpotPortfolioStatus = "active";
  paperFuturesPortfolioStatus = "active";
}


function hasValidInternalServiceAuth(request) {
  const expected = process.env.OIDC_INTERNAL_SERVICE_SECRET ?? "";
  const supplied = request.headers["x-zak-internal-service-secret"];

  if (
    expected.length < 32 ||
    typeof supplied !== "string"
  ) {
    return false;
  }

  const expectedBytes = Buffer.from(expected, "utf8");
  const suppliedBytes = Buffer.from(supplied, "utf8");

  return (
    expectedBytes.length === suppliedBytes.length &&
    timingSafeEqual(expectedBytes, suppliedBytes)
  );
}

function sendJson(response, status, payload) {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });

  response.end(JSON.stringify(payload));
}

async function readJson(request) {
  let raw = "";

  for await (const chunk of request) {
    raw += chunk.toString("utf8");

    if (raw.length > 64 * 1024) {
      throw new Error("Request body too large.");
    }
  }

  return JSON.parse(raw);
}

function stateCookieName(state) {
  const hash = createHash("sha256")
    .update(Buffer.from("state", "ascii"))
    .update(Buffer.from([0]))
    .update(Buffer.from(state, "ascii"))
    .digest("hex");

  return `__Host-zak_oidc_${hash.slice(0, 24)}`;
}

function randomToken() {
  return randomBytes(32).toString("base64url");
}

const server = createServer(async (request, response) => {
  const url = new URL(
    request.url ?? "/",
    `http://${request.headers.host ?? `${HOST}:${PORT}`}`,
  );

  try {
    if (request.method === "GET" && url.pathname === "/__health") {
      return sendJson(response, 200, { ok: true });
    }


    if (
      request.method === "GET" &&
      url.pathname === "/__test__/fail-revoke"
    ) {
      failRevoke = url.searchParams.get("enabled") === "true";
      return sendJson(response, 200, { failRevoke });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/session-validation-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = ["valid", "unavailable", "malformed"];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid session validation mode.",
        });
      }

      sessionValidationMode = mode;

      return sendJson(response, 200, {
        mode: sessionValidationMode,
      });
    }

    if (
      request.method === "POST" &&
      url.pathname === "/__test__/session-state"
    ) {
      const body = await readJson(request);

      if (
        typeof body.session_token !== "string" ||
        !["revoke", "expire"].includes(body.action)
      ) {
        return sendJson(response, 400, {
          detail: "Invalid session state request.",
        });
      }

      const session = sessions.get(body.session_token);

      if (!session) {
        return sendJson(response, 404, {
          detail: "Mock session not found.",
        });
      }

      if (body.action === "revoke") {
        session.revoked = true;
      } else {
        session.expiresAt = new Date(Date.now() - 1000).toISOString();
      }

      return sendJson(response, 200, {
        updated: true,
        action: body.action,
      });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/oidc-transaction-response"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "malformed",
        "expired",
        "wrong-host",
        "wrong-client",
        "wrong-callback",
        "duplicate-parameter",
      ];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid OIDC transaction response mode.",
        });
      }

      oidcTransactionResponseMode = mode;
      return sendJson(response, 200, {
        mode: oidcTransactionResponseMode,
      });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/redirect-internal"
    ) {
      redirectInternal = url.searchParams.get("enabled") === "true";
      redirectTargetHits = 0;
      return sendJson(response, 200, { redirectInternal });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/redirect-target"
    ) {
      redirectTargetHits += 1;
      return sendJson(response, 200, { reached: true });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/redirect-target-count"
    ) {
      return sendJson(response, 200, { hits: redirectTargetHits });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/market-data-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = ["valid", "unauthorized", "unavailable"];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid market-data mock mode.",
        });
      }

      marketDataMode = mode;
      return sendJson(response, 200, { mode: marketDataMode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-spot-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "unauthorized",
        "forbidden",
        "not-found",
        "conflict",
        "unprocessable",
        "unavailable",
        "malformed",
      ];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid paper Spot mock mode.",
        });
      }

      paperSpotMode = mode;
      return sendJson(response, 200, { mode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-spot-captures"
    ) {
      return sendJson(response, 200, {
        captures: paperSpotCaptures.splice(0),
      });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-futures-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "unauthorized",
        "forbidden",
        "not-found",
        "conflict",
        "unprocessable",
        "unavailable",
        "malformed",
      ];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid paper Futures mock mode.",
        });
      }

      paperFuturesMode = mode;
      return sendJson(response, 200, { mode: paperFuturesMode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-futures-captures"
    ) {
      return sendJson(response, 200, {
        captures: paperFuturesCaptures.splice(0),
      });
    }
    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-manual-top-up-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "unauthorized",
        "forbidden",
        "not-found",
        "conflict",
        "unprocessable",
        "unavailable",
        "malformed",
      ];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid Manual Top-up mock mode.",
        });
      }

      paperManualTopUpMode.initial = mode;
      return sendJson(response, 200, { mode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-manual-top-up-captures"
    ) {
      return sendJson(response, 200, {
        captures: paperManualTopUpCaptures.splice(0),
      });
    }
    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-bootstrap-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "unauthorized",
        "forbidden",
        "not-found",
        "conflict",
        "unavailable",
        "futures-positions",        "spot-positions",

        "manual-top-up-eligible",
        "manual-top-up-admin",
        "manual-top-up-trader",
        "manual-top-up-viewer",
        "manual-top-up-over-limit-spot",
        "manual-top-up-over-limit-futures",
        "manual-top-up-disabled-spot",
        "manual-top-up-disabled-futures",
        "malformed",
      ];
      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid paper bootstrap mock mode.",
        });
      }
      paperBootstrapMode = mode;
      const lowBalanceModes = [
        "manual-top-up-eligible",
        "manual-top-up-admin",
        "manual-top-up-trader",
        "manual-top-up-viewer",
        "manual-top-up-over-limit-spot",
        "manual-top-up-over-limit-futures",
        "manual-top-up-disabled-spot",
        "manual-top-up-disabled-futures",
      ];
      paperSpotCashAvailable =
        mode === "manual-top-up-over-limit-spot"
          ? "100.01"
          : lowBalanceModes.includes(mode)
            ? "100.00"
            : "1000.00";
      paperFuturesCashAvailable =
        mode === "manual-top-up-over-limit-futures"
          ? "100.01"
          : lowBalanceModes.includes(mode)
            ? "100.00"
            : "1000.00";
      paperSpotPortfolioStatus =
        mode === "manual-top-up-disabled-spot" ? "disabled" : "active";
      paperFuturesPortfolioStatus =
        mode === "manual-top-up-disabled-futures" ? "disabled" : "active";
      return sendJson(response, 200, { mode: paperBootstrapMode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-bootstrap-captures"
    ) {
      return sendJson(response, 200, {
        captures: paperBootstrapCaptures.splice(0),
      });
    }
    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-reset-mode"
    ) {
      const mode = url.searchParams.get("mode");
      const allowedModes = [
        "valid",
        "unauthorized",
        "forbidden",
        "not-found",
        "conflict",
        "unprocessable",
        "unavailable",
        "malformed",
      ];

      if (!allowedModes.includes(mode ?? "")) {
        return sendJson(response, 400, {
          detail: "Invalid paper reset mock mode.",
        });
      }

      resetPaperResetFixtures();
      paperResetMode.initial = mode;

      return sendJson(response, 200, { mode });
    }

    if (
      request.method === "GET" &&
      url.pathname === "/__test__/paper-reset-captures"
    ) {
      return sendJson(response, 200, {
        captures: paperResetCaptures.splice(0),
      });
    }


    if (
      request.method === "GET" &&
      url.pathname === "/api/v1/paper-trading/bootstrap"
    ) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      const cookieHeader = request.headers.cookie ?? "";
      const cookieMatch = cookieHeader.match(
        /(?:^|;\s*)zak_session=([A-Za-z0-9_-]{32,512})(?:;|$)/,
      );
      const session = cookieMatch
        ? sessions.get(cookieMatch[1])
        : undefined;

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid backend session.",
        });
      }

      paperBootstrapCaptures.push({
        path: url.pathname,
        sessionAuthenticated: true,
      });

      const failureStatuses = {
        unauthorized: 401,
        forbidden: 403,
        "not-found": 404,
        conflict: 409,
        unavailable: 503,
      };

      if (paperBootstrapMode === "malformed") {
        return sendJson(response, 200, {
          account: { id: "bad" },
          portfolios: [],
        });
      }

      if (failureStatuses[paperBootstrapMode]) {
        return sendJson(response, failureStatuses[paperBootstrapMode], {
          detail: "Mock bootstrap failure.",
        });
      }

      return sendJson(response, 200, {
        account: {
          id: "11111111-1111-4111-8111-111111111111",
          name: "Primary Account",
          role:
            paperBootstrapMode === "manual-top-up-admin"
              ? "admin"
              : paperBootstrapMode === "manual-top-up-trader"
                ? "trader"
                : paperBootstrapMode === "manual-top-up-viewer"
                  ? "viewer"
                  : "owner",
        },
        portfolios: [
          {
            id: "22222222-2222-4222-8222-222222222222",
            market_type: "spot",
            status: paperSpotPortfolioStatus,
            position_mode: null,
            session: {
              id: paperSpotSession.id,
              session_number: paperSpotSession.sessionNumber,
              status: "active",
            },
            ledger: {
              book_id: paperSpotSession.bookId,
              cash_available: paperSpotCashAvailable,
              spot_position_cost: "0",
              futures_margin_locked: "0",
              realized_pnl: "0",
              fee_expense: "0",
              contributed_capital: "1000.00",
            },
            positions:
              paperBootstrapMode === "spot-positions"
                ? [
                    {
                      id: "99999999-9999-4999-8999-999999999991",
                      symbol: "BTCUSDT",
                      side: "long",
                      status: "open",
                      leverage: null,
                      initial_quantity: "0.01",
                      remaining_quantity: "0.01",
                      entry_price: "100.00",
                      remaining_cost_basis: "1.00",
                      margin_locked: null,
                      realized_pnl: "0",
                      entry_fee_original: "0.001000",
                      entry_fee_posted: "0.01",
                      opened_at: "2026-11-22T10:00:00Z",
                      closed_at: null,
                    },
                  ]
                : [],
          },
          {
            id: "55555555-5555-4555-8555-555555555555",
            market_type: "futures",
            status: paperFuturesPortfolioStatus,
            position_mode: "hedge",
            session: {
              id: paperFuturesSession.id,
              session_number: paperFuturesSession.sessionNumber,
              status: "active",
            },
            ledger: {
              book_id: paperFuturesSession.bookId,
              cash_available: paperFuturesCashAvailable,
              spot_position_cost: "0",
              futures_margin_locked: "0",
              realized_pnl: "0",
              fee_expense: "0",
              contributed_capital: "1000.00",
            },
            positions:
              paperBootstrapMode === "futures-positions"
                ? [
                    {
                      id: "88888888-8888-4888-8888-888888888881",
                      symbol: "BTCUSDT",
                      side: "long",
                      status: "open",
                      leverage: "5",
                      initial_quantity: "0.02",
                      remaining_quantity: "0.02",
                      entry_price: "100.00",
                      remaining_cost_basis: null,
                      margin_locked: "0.40",
                      realized_pnl: "0",
                      entry_fee_original: "0.008000",
                      entry_fee_posted: "0.01",
                      opened_at: "2026-11-22T10:00:00Z",
                      closed_at: null,
                    },
                    {
                      id: "88888888-8888-4888-8888-888888888882",
                      symbol: "ETHUSDT",
                      side: "short",
                      status: "open",
                      leverage: "3",
                      initial_quantity: "0.50",
                      remaining_quantity: "0.50",
                      entry_price: "50.00",
                      remaining_cost_basis: null,
                      margin_locked: "8.33",
                      realized_pnl: "0",
                      entry_fee_original: "0.010000",
                      entry_fee_posted: "0.01",
                      opened_at: "2026-11-22T10:05:00Z",
                      closed_at: null,
                    },
                  ]
                : [],
          },
        ],
      });
    }

    const spotTradeMatch = url.pathname.match(
      /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/paper-trades\/spot$/,
    );

    if (request.method === "POST" && spotTradeMatch) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      const cookieHeader = request.headers.cookie ?? "";
      const cookieMatch = cookieHeader.match(
        /(?:^|;\s*)zak_session=([A-Za-z0-9_-]{32,512})(?:;|$)/,
      );

      const session = cookieMatch
        ? sessions.get(cookieMatch[1])
        : undefined;

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid backend session.",
        });
      }

      const body = await readJson(request);

      paperSpotCaptures.push({
        path: url.pathname,
        body,
        idempotencyKey:
          request.headers["idempotency-key"] ?? null,
        sessionAuthenticated: true,
      });

      const failureStatuses = {
        unauthorized: 401,
        forbidden: 403,
        "not-found": 404,
        conflict: 409,
        unprocessable: 422,
        unavailable: 503,
      };

      if (paperSpotMode === "malformed") {
        response.writeHead(200, {
          "content-type": "application/json",
          "cache-control": "no-store",
        });
        response.end("{invalid-json");
        return;
      }

      if (failureStatuses[paperSpotMode]) {
        return sendJson(
          response,
          failureStatuses[paperSpotMode],
          { detail: "Mock Spot trade rejection." },
        );
      }

      return sendJson(response, 200, {
        trade_id: randomUUID(),
        status: "accepted",
        replayed: false,
      });
    }
    const manualTopUpMatch = url.pathname.match(
      /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/manual-top-up$/,
    );

    if (request.method === "POST" && manualTopUpMatch) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      const cookieHeader = request.headers.cookie ?? "";
      const cookieMatch = cookieHeader.match(
        /(?:^|;\s*)zak_session=([A-Za-z0-9_-]{32,512})(?:;|$)/,
      );

      const session = cookieMatch
        ? sessions.get(cookieMatch[1])
        : undefined;

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid backend session.",
        });
      }

      const body = await readJson(request);

      paperManualTopUpCaptures.push({
        path: url.pathname,
        body,
        idempotencyKey: request.headers["idempotency-key"] ?? null,
        sessionAuthenticated: true,
      });

      const failureStatuses = {
        unauthorized: 401,
        forbidden: 403,
        "not-found": 404,
        conflict: 409,
        unprocessable: 422,
        unavailable: 503,
      };

      if (paperManualTopUpMode.initial === "malformed") {
        response.writeHead(200, {
          "content-type": "application/json",
          "cache-control": "no-store",
        });
        response.end("{invalid-json");
        return;
      }

      if (failureStatuses[paperManualTopUpMode.initial]) {
        return sendJson(
          response,
          failureStatuses[paperManualTopUpMode.initial],
          { detail: "Mock Manual Top-up rejection." },
        );
      }

      const isFuturesPortfolio = url.pathname.includes(
        "/portfolios/55555555-5555-4555-8555-555555555555/",
      );
      const marketType = isFuturesPortfolio ? "futures" : "spot";
      const previousBalance = isFuturesPortfolio
        ? paperFuturesCashAvailable
        : paperSpotCashAvailable;
      const previousCents = BigInt(previousBalance.replace(".", ""));
      const amountCents = 100000n - previousCents;

      if (previousCents > 10000n || amountCents <= 0n) {
        return sendJson(response, 409, {
          detail: "Mock portfolio is not eligible for Manual Top-up.",
        });
      }

      const amount = `${amountCents / 100n}.${String(amountCents % 100n).padStart(2, "0")}`;

      if (isFuturesPortfolio) {
        paperFuturesCashAvailable = "1000.00";
      } else {
        paperSpotCashAvailable = "1000.00";
      }

      return sendJson(response, 200, {
        transaction_id: randomUUID(),
        book_id: isFuturesPortfolio
          ? "77777777-7777-4777-8777-777777777777"
          : "44444444-4444-4444-8444-444444444444",
        market_type: marketType,
        transaction_type: "manual_top_up",
        status: "posted",
        entry_count: 2,
        debit_total: amount,
        credit_total: amount,
        amount,
        previous_balance: previousBalance,
        new_balance: "1000.00",
        replayed: false,
      });
    }
    const paperResetMatch = url.pathname.match(
      /^\/api\/v1\/accounts\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/portfolios\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/reset$/,
    );

    if (request.method === "POST" && paperResetMatch) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      const cookieHeader = request.headers.cookie ?? "";
      const cookieMatch = cookieHeader.match(
        /(?:^|;\s*)zak_session=([A-Za-z0-9_-]{32,512})(?:;|$)/,
      );

      const session = cookieMatch
        ? sessions.get(cookieMatch[1])
        : undefined;

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid backend session.",
        });
      }

      const accountId = paperResetMatch[1].toLowerCase();
      const portfolioId = paperResetMatch[2].toLowerCase();

      if (accountId !== "11111111-1111-4111-8111-111111111111") {
        return sendJson(response, 404, {
          detail: "Mock account not found.",
        });
      }

      const isSpotPortfolio =
        portfolioId === "22222222-2222-4222-8222-222222222222";
      const isFuturesPortfolio =
        portfolioId === "55555555-5555-4555-8555-555555555555";

      if (!isSpotPortfolio && !isFuturesPortfolio) {
        return sendJson(response, 404, {
          detail: "Mock portfolio not found.",
        });
      }

      const body = await readJson(request);
      const idempotencyKey = request.headers["idempotency-key"] ?? null;

      paperResetCaptures.push({
        path: url.pathname,
        body,
        idempotencyKey,
        sessionAuthenticated: true,
      });

      if (
        typeof idempotencyKey !== "string" ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(idempotencyKey)
      ) {
        return sendJson(response, 422, {
          detail: "A canonical UUIDv4 Idempotency-Key is required.",
        });
      }

      if (
        body === null ||
        typeof body !== "object" ||
        Array.isArray(body) ||
        Object.keys(body).length !== 1 ||
        body.confirmed !== true
      ) {
        return sendJson(response, 422, {
          detail: "Explicit reset confirmation is required.",
        });
      }

      if (paperResetMode.initial === "malformed") {
        response.writeHead(200, {
          "content-type": "application/json",
          "cache-control": "no-store",
        });
        response.end("{invalid-json");
        return;
      }

      const failureStatuses = {
        unauthorized: 401,
        forbidden: 403,
        "not-found": 404,
        conflict: 409,
        unprocessable: 422,
        unavailable: 503,
      };

      if (failureStatuses[paperResetMode.initial]) {
        return sendJson(
          response,
          failureStatuses[paperResetMode.initial],
          { detail: "Mock Portfolio Reset rejection." },
        );
      }

      const replayKey = `${portfolioId}:${idempotencyKey}`;
      const previousReplay = paperResetReplays.get(replayKey);

      if (previousReplay) {
        return sendJson(response, 200, {
          ...previousReplay,
          replayed: true,
        });
      }

      const targetSession = isSpotPortfolio
        ? paperSpotSession
        : paperFuturesSession;

      const previousSessionId = targetSession.id;
      const previousSessionNumber = targetSession.sessionNumber;
      const previousBookId = targetSession.bookId;
      const previousSessionArchivedAt = new Date().toISOString();

      const newSessionId = randomUUID();
      const newBookId = randomUUID();
      const newSessionNumber = previousSessionNumber + 1;

      if (isSpotPortfolio) {
        paperSpotSession = {
          id: newSessionId,
          sessionNumber: newSessionNumber,
          bookId: newBookId,
        };
        paperSpotCashAvailable = "1000.00";
      } else {
        paperFuturesSession = {
          id: newSessionId,
          sessionNumber: newSessionNumber,
          bookId: newBookId,
        };
        paperFuturesCashAvailable = "1000.00";
      }

      const result = {
        operation_type: "reset",
        status: "completed",
        account_id: accountId,
        portfolio_id: portfolioId,
        market_type: isSpotPortfolio ? "spot" : "futures",
        previous_session_id: previousSessionId,
        previous_session_number: previousSessionNumber,
        previous_book_id: previousBookId,
        previous_session_archived_at: previousSessionArchivedAt,
        new_session_id: newSessionId,
        new_session_number: newSessionNumber,
        new_book_id: newBookId,
        initial_balance: "1000.00",
        replayed: false,
      };

      paperResetReplays.set(replayKey, result);

      return sendJson(response, 200, result);
    }
    const futuresTradeMatch = url.pathname.match(
      /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/paper-trades\/futures$/,
    );

    if (request.method === "POST" && futuresTradeMatch) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      const cookieHeader = request.headers.cookie ?? "";
      const cookieMatch = cookieHeader.match(
        /(?:^|;\s*)zak_session=([A-Za-z0-9_-]{32,512})(?:;|$)/,
      );

      const session = cookieMatch
        ? sessions.get(cookieMatch[1])
        : undefined;

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid backend session.",
        });
      }

      const body = await readJson(request);

      paperFuturesCaptures.push({
        path: url.pathname,
        body,
        idempotencyKey: request.headers["idempotency-key"] ?? null,
        sessionAuthenticated: true,
      });

      const failureStatuses = {
        unauthorized: 401,
        forbidden: 403,
        "not-found": 404,
        conflict: 409,
        unprocessable: 422,
        unavailable: 503,
      };

      if (paperFuturesMode === "malformed") {
        response.writeHead(200, {
          "content-type": "application/json",
          "cache-control": "no-store",
        });
        response.end("{invalid-json");
        return;
      }

      if (failureStatuses[paperFuturesMode]) {
        return sendJson(
          response,
          failureStatuses[paperFuturesMode],
          { detail: "Mock Futures trade rejection." },
        );
      }

      const positionId = body.position_id ?? randomUUID();
      const isReduceOnly = body.reduce_only === true;

      return sendJson(response, 200, {
        transaction_id: randomUUID(),
        book_id: "77777777-7777-4777-8777-777777777777",
        market_type: "futures",
        transaction_type: isReduceOnly ? "futures_close" : "futures_open",
        status: "posted",
        entry_count: 2,
        debit_total: "0.04",
        credit_total: "0.04",
        position_id: positionId,
        symbol: body.symbol,
        side: body.side,
        position_side: body.position_side,
        position_action: isReduceOnly ? "reduce" : "open",
        quantity: body.quantity,
        execution_price: "100.00",
        gross_value: "100.00",
        fee_original: "0.040000",
        fee_posted: "0.04",
        realized_pnl: "0",
        position_realized_pnl: "0",
        remaining_quantity: body.quantity,
        remaining_margin: "10.04",
        entry_price: "100.00",
        leverage: body.leverage,
        position_status: "open",
        quote_source: "futures_depth_book",
        quote_update_id: 1,
        fee_config_snapshot: {
          schema_version: 1,
          policy_id: "mock-futures-fee-policy",
          version: 1,
          market_type: "futures",
          entry_fee_rate: "0.0004",
          exit_fee_rate: "0.0004",
        },
        symbol_metadata_snapshot: {
          schema_version: 1,
          market: "usdm_futures",
          symbol: body.symbol,
          status: "TRADING",
          base_asset: "BTC",
          quote_asset: "USDT",
          pair: "BTCUSDT",
          contract_type: "PERPETUAL",
          margin_asset: "USDT",
          delivery_date: null,
          onboard_date: 1502942400000,
          order_types: ["LIMIT", "MARKET"],
          filter_types: ["PRICE_FILTER", "LOT_SIZE"],
          price_precision: 2,
          quantity_precision: 3,
          filters: [
            {
              filterType: "PRICE_FILTER",
              tickSize: "0.10",
            },
          ],
          metadata_server_time_ms: 1730000000000,
          tick_size: "0.10",
        },
        replayed: false,
      });
    }
    if (url.pathname.startsWith("/api/v1/market-data/")) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      if (marketDataMode === "unauthorized") {
        return sendJson(response, 401, {
          detail: "Mock market-data authorization rejected.",
        });
      }

      if (marketDataMode === "unavailable") {
        return sendJson(response, 503, {
          detail: "Mock market-data service unavailable.",
        });
      }

      if (
        request.method === "GET" &&
        url.pathname === "/api/v1/market-data/health"
      ) {
        return sendJson(response, 200, { status: "ok" });
      }

      const latestMatch = url.pathname.match(
        /^\/api\/v1\/market-data\/latest\/(spot|futures_mark_price)\/([A-Z0-9]{2,30})$/,
      );

      if (request.method === "GET" && latestMatch) {
        return sendJson(response, 200, {
          group: latestMatch[1],
          symbol: latestMatch[2],
          source: "mock",
        });
      }

      return sendJson(response, 404, {
        detail: "Mock market-data endpoint not found.",
      });
    }
    if (url.pathname.startsWith("/internal/v1/auth/")) {
      if (!hasValidInternalServiceAuth(request)) {
        return sendJson(response, 401, {
          detail: "Internal service authentication required.",
        });
      }

      if (redirectInternal) {
        redirectInternal = false;
        response.writeHead(302, {
          location: `http://${HOST}:${PORT}/__test__/redirect-target`,
          "cache-control": "no-store",
        });
        response.end();
        return;
      }
    }

    if (
      request.method === "POST" &&
      url.pathname === "/internal/v1/auth/oidc/transactions"
    ) {
      const body = await readJson(request);

      if (
        typeof body.request_id !== "string" ||
        !/^[0-9a-f-]{36}$/i.test(body.request_id)
      ) {
        return sendJson(response, 422, {
          detail: "Invalid request_id.",
        });
      }

      const state = randomToken();
      const nonce = randomToken();
      const browserBinding = randomToken();
      const codeChallenge = randomToken();

      const authorizationUrl = new URL(
        "https://example.auth0.com/authorize",
      );

      authorizationUrl.searchParams.set("response_type", "code");
      authorizationUrl.searchParams.set("client_id", "zak-e2e-client");
      authorizationUrl.searchParams.set("redirect_uri", CALLBACK_URL);
      authorizationUrl.searchParams.set("scope", "openid email profile");
      authorizationUrl.searchParams.set("state", state);
      authorizationUrl.searchParams.set("nonce", nonce);
      authorizationUrl.searchParams.set("code_challenge", codeChallenge);
      authorizationUrl.searchParams.set(
        "code_challenge_method",
        "S256",
      );

      transactions.set(state, {
        browserBinding,
        createdAt: Date.now(),
      });

      const transactionResponse = {
        authorization_url: authorizationUrl.toString(),
        state,
        browser_binding_cookie: {
          name: stateCookieName(state),
          value: browserBinding,
          max_age_seconds: 300,
        },
        expires_at: new Date(Date.now() + 300_000).toISOString(),
      };

      switch (oidcTransactionResponseMode) {
        case "malformed":
          transactionResponse.unexpected_field = "must-be-rejected";
          break;

        case "expired":
          transactionResponse.expires_at =
            new Date(Date.now() - 1_000).toISOString();
          break;

        case "wrong-host": {
          const invalidUrl = new URL(transactionResponse.authorization_url);
          invalidUrl.hostname = "attacker.example";
          transactionResponse.authorization_url = invalidUrl.toString();
          break;
        }

        case "wrong-client": {
          const invalidUrl = new URL(transactionResponse.authorization_url);
          invalidUrl.searchParams.set("client_id", "attacker-client");
          transactionResponse.authorization_url = invalidUrl.toString();
          break;
        }

        case "wrong-callback": {
          const invalidUrl = new URL(transactionResponse.authorization_url);
          invalidUrl.searchParams.set(
            "redirect_uri",
            "https://attacker.example/callback",
          );
          transactionResponse.authorization_url = invalidUrl.toString();
          break;
        }

        case "duplicate-parameter": {
          const invalidUrl = new URL(transactionResponse.authorization_url);
          invalidUrl.searchParams.append("client_id", "zak-e2e-client");
          transactionResponse.authorization_url = invalidUrl.toString();
          break;
        }
      }

      return sendJson(response, 201, transactionResponse);
    }

    if (
      request.method === "POST" &&
      url.pathname === "/internal/v1/auth/oidc/callback"
    ) {
      const body = await readJson(request);

      if (
        typeof body.state !== "string" ||
        typeof body.authorization_code !== "string" ||
        typeof body.browser_binding !== "string"
      ) {
        return sendJson(response, 422, {
          detail: "Invalid callback request.",
        });
      }

      const transaction = transactions.get(body.state);

      if (
        !transaction ||
        transaction.createdAt + 300_000 <= Date.now() ||
        transaction.browserBinding !== body.browser_binding ||
        body.authorization_code !== "mock-authorization-code"
      ) {
        return sendJson(response, 401, {
          detail: "Invalid or expired OIDC transaction.",
        });
      }

      // Consume the transaction once to model replay protection.
      transactions.delete(body.state);

      const token = randomToken();
      const user = {
        id: randomUUID(),
        email: "zak-e2e@example.test",
      };

      const expiresAt = new Date(
        Date.now() + 12 * 60 * 60 * 1000,
      ).toISOString();

      

      sessions.set(token, {
        user,
        expiresAt,
        revoked: false,
      });

      return sendJson(response, 200, {
        session: {
          token,
          expires_at: expiresAt,
        },
        user,
      });
    }

    if (
      request.method === "POST" &&
      url.pathname === "/internal/v1/auth/session/validate"
    ) {
      if (sessionValidationMode === "unavailable") {
        return sendJson(response, 503, {
          detail: "Mock session validation unavailable.",
        });
      }

      if (sessionValidationMode === "malformed") {
        return sendJson(response, 200, {
          authenticated: true,
          user: {
            id: "not-a-valid-uuid",
            email: "not-an-email",
          },
          session: {
            expires_at: "not-a-valid-datetime",
          },
        });
      }

      const body = await readJson(request);
      const session = sessions.get(body.session_token);

      if (
        !session ||
        session.revoked ||
        Date.parse(session.expiresAt) <= Date.now()
      ) {
        return sendJson(response, 401, {
          detail: "Invalid session.",
        });
      }

      return sendJson(response, 200, {
        authenticated: true,
        user: session.user,
        session: {
          expires_at: session.expiresAt,
        },
      });
    }

    if (
      request.method === "POST" &&
      url.pathname === "/internal/v1/auth/session/revoke"
    ) {
      const body = await readJson(request);

      if (failRevoke) {
        return sendJson(response, 503, {
          detail: "Mock revoke service unavailable.",
        });
      }

      const session = sessions.get(body.session_token);

      if (session) {
        session.revoked = true;
      }

      return sendJson(response, 200, {
        revoked: Boolean(session),
      });
    }

    return sendJson(response, 404, {
      detail: "Mock endpoint not found.",
    });
  } catch {
    return sendJson(response, 400, {
      detail: "Invalid mock request.",
    });
  }
});

server.listen(PORT, HOST, () => {
  process.stdout.write(
    `ZaK E2E mock API listening on http://${HOST}:${PORT}\n`,
  );
});

function shutdown() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

