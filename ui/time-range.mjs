const LOCAL_DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

function parseLocalDate(value, fieldName) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return undefined;

  const match = LOCAL_DATE_PATTERN.exec(normalized);
  if (!match) {
    throw new Error(`${fieldName}必须是有效日期。`);
  }

  const [, yearText, monthText, dayText] = match;
  const year = Number(yearText);
  const monthIndex = Number(monthText) - 1;
  const day = Number(dayText);
  const date = new Date(year, monthIndex, day, 0, 0, 0, 0);
  if (
    date.getFullYear() !== year
    || date.getMonth() !== monthIndex
    || date.getDate() !== day
  ) {
    throw new Error(`${fieldName}必须是有效日期。`);
  }
  return { date, year, monthIndex, day };
}

export function dateInputsToEpochRange(startValue, endValue) {
  const start = parseLocalDate(startValue, "开始日期");
  const end = parseLocalDate(endValue, "结束日期");
  const range = {};

  if (start) {
    range.startTime = start.date.getTime();
  }
  if (end) {
    range.endTime = new Date(
      end.year,
      end.monthIndex,
      end.day + 1,
      0,
      0,
      0,
      0,
    ).getTime() - 1;
  }
  if (
    range.startTime !== undefined
    && range.endTime !== undefined
    && range.startTime > range.endTime
  ) {
    throw new Error("开始日期不能晚于结束日期。");
  }
  for (const value of Object.values(range)) {
    if (!Number.isSafeInteger(value)) {
      throw new Error("日期无法转换为 Unix epoch 毫秒整数。");
    }
  }
  return range;
}
