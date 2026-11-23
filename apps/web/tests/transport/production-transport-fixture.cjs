function buildTransport() {
  return {
    async fetch() {
      return new Response("factory-transport", { status: 200 });
    },
  };
}

function buildInvalidTransport() {
  return {
    fetch: "not-a-function",
  };
}

function throwSensitiveError() {
  throw new Error("fixture-sensitive-secret-detail");
}

module.exports = {
  buildTransport,
  buildInvalidTransport,
  throwSensitiveError,
};
