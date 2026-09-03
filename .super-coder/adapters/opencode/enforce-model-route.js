// Refuse a controlled OpenCode route mismatch before provider dispatch.
//
// run.py sets SC_OPENCODE_ENFORCED_MODEL only for an interactive host Admin
// launch with an explicit OpenCode --model request. OpenCode's chat.params
// hook receives the resolved runtime Model immediately before its LLM request;
// throwing here prevents the harness from responding or requesting tools.

function observedRoute(model) {
  const provider = model && model.providerID;
  const id = model && (model.id || model.modelID);
  if (typeof provider !== "string" || !provider || typeof id !== "string" || !id) {
    return null;
  }
  return `${provider}/${id}`;
}

export const EnforceModelRoute = async () => ({
  "chat.params": async (input) => {
    const encoded = process.env.SC_OPENCODE_ENFORCED_MODEL;
    if (!encoded) return;

    let contract;
    try {
      contract = JSON.parse(encoded);
    } catch {
      throw new Error(
        "Controlled OpenCode model route refused before provider dispatch: " +
        "requested=unavailable observed=unavailable (invalid launch contract)",
      );
    }
    const requested = contract && contract.requested;
    const expected = contract && contract.selector;
    if (typeof requested !== "string" || !requested ||
        typeof expected !== "string" || !expected) {
      throw new Error(
        "Controlled OpenCode model route refused before provider dispatch: " +
        "requested=unavailable observed=unavailable (invalid launch contract)",
      );
    }

    const observed = observedRoute(input && input.model);
    const evidence = `requested=${requested} observed=${observed || "unavailable"}`;
    if (observed !== expected) {
      throw new Error(
        "Controlled OpenCode model route refused before provider dispatch: " +
        `${evidence} expected=${expected}`,
      );
    }
    console.error(`Subfloor OpenCode model route observed: ${evidence}`);
  },
});
