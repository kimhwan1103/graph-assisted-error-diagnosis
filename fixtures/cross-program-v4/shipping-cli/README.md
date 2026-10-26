# Shipping CLI
Run `node bin/quote.js` with an optional JSON argument containing integer amount and weightKg.
Default input is amount 4000 and weightKg 2. Negative values or malformed input exit with code 2 and INVALID_INPUT on stderr.
The application reads tariffs and prints a JSON shipping_fee and label. OPTIONAL_CONFIG=1 enables optional local labels; missing labels use the shipping default.
