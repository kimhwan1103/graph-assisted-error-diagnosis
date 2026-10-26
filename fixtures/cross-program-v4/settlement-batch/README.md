# Settlement batch
Run `node src/job.js` with an optional JSON transaction array argument. Without it, fixtures/transactions.json is used.
Amounts are nonnegative integer currency units. Round each transaction fee to the nearest integer, ties upward; aggregate the individual fees and net amounts.
Print count, fees, settlement, label JSON to stdout. Invalid transactions exit 2 with INVALID_TRANSACTIONS on stderr.
OPTIONAL_CONFIG=1 enables optional labels and falls back to settlement when missing. No output files are written.
