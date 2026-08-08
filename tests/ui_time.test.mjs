import assert from "node:assert/strict";
import test from "node:test";

import { dateInputsToEpochRange } from "../ui/time-range.mjs";

test("blank date inputs are omitted from the request", () => {
  assert.deepEqual(dateInputsToEpochRange("", ""), {});
});

test("local date inputs become inclusive Unix epoch millisecond boundaries", () => {
  const range = dateInputsToEpochRange("2026-08-08", "2026-08-08");
  assert.deepEqual(range, {
    startTime: new Date(2026, 7, 8, 0, 0, 0, 0).getTime(),
    endTime: new Date(2026, 7, 9, 0, 0, 0, 0).getTime() - 1,
  });
  assert.equal(Number.isSafeInteger(range.startTime), true);
  assert.equal(Number.isSafeInteger(range.endTime), true);
});

test("end boundary uses the next local calendar midnight", () => {
  const range = dateInputsToEpochRange("", "2026-03-08");
  assert.equal(range.endTime, new Date(2026, 2, 9, 0, 0, 0, 0).getTime() - 1);
});

test("invalid and reversed ranges are rejected before invoking the connector", () => {
  assert.throws(
    () => dateInputsToEpochRange("2026-02-30", ""),
    /开始日期必须是有效日期/,
  );
  assert.throws(
    () => dateInputsToEpochRange("2026-08-09", "2026-08-08"),
    /开始日期不能晚于结束日期/,
  );
});
