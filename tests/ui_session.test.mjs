import assert from "node:assert/strict";
import test from "node:test";

import { sessionHistoryAvailable } from "../ui/session-model.mjs";

test("sessions backed by message tables remain readable", () => {
  assert.equal(sessionHistoryAvailable({ historyAvailable: true }), true);
});

test("sessions without message tables are summary-only", () => {
  assert.equal(sessionHistoryAvailable({ historyAvailable: false }), false);
});

test("older connector responses remain readable by default", () => {
  assert.equal(sessionHistoryAvailable({ conversationId: "legacy" }), true);
});
