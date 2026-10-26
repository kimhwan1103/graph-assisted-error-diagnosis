export function aggregate(transactions, policy) {
  if (!Array.isArray(transactions) || transactions.some(t => !Number.isSafeInteger(t.amount) || t.amount < 0)) {
    throw new TypeError('INVALID_TRANSACTIONS');
  }
  if (!Number.isSafeInteger(policy.feeBasisPoints) || policy.feeBasisPoints < 0 || policy.feeBasisPoints > 10000) {
    throw new TypeError('INVALID_POLICY');
  }
  let fees = 0, settlement = 0;
  for (const transaction of transactions) {
    const fee = Math.floor((transaction.amount * policy.feeBasisPoints + 5000) / 10000);
    fees += fee;
    settlement += transaction.amount - fee;
  }
  return { count: transactions.length, fees, settlement };
}
