export function calculate(order, tariffs) {
  if (!order || !Number.isSafeInteger(order.amount) || !Number.isSafeInteger(order.weightKg)
      || order.amount < 0 || order.weightKg < 0) {
    throw new TypeError('INVALID_INPUT');
  }
  if (![tariffs.base, tariffs.perKg, tariffs.freeAt].every(x => Number.isSafeInteger(x) && x >= 0)) {
    throw new TypeError('INVALID_TARIFFS');
  }
  return order.amount >= tariffs.freeAt ? 0 : tariffs.base + order.weightKg * tariffs.perKg;
}
