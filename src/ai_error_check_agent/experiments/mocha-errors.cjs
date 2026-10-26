// Observation-only reporter for historical Mocha versions, executed in Docker.
// Read the actual failure event; never reconstruct an error from a reference fix.
'use strict';
var Base = require('/deps/node_modules/mocha/lib/reporters/base');
var fs = require('fs');

module.exports = function ErrorPreservingReporter(runner) {
  Base.call(this, runner);
  var reporter = this;
  var tests = [], failures = [], passes = [], pending = [];
  var errors = new Map();
  runner.on('test end', function(test) { tests.push(test); });
  runner.on('pass', function(test) { passes.push(test); });
  runner.on('pending', function(test) { pending.push(test); });
  runner.on('fail', function(test, err) {
    failures.push(test);
    var details = {};
    // Error.message and Error.stack are normally non-enumerable.
    Object.getOwnPropertyNames(err).concat(['name', 'message', 'stack']).forEach(function(key) {
      var value = err[key];
      if (value === undefined) return;
      try { details[key] = JSON.parse(JSON.stringify(value)); }
      catch (_) { details[key] = String(value); }
    });
    errors.set(test, details);
  });
  function clean(test) {
    return {
      title: test.title,
      fullTitle: test.fullTitle(),
      duration: test.duration,
      err: errors.get(test) || {}
    };
  }
  runner.on('end', function() {
    fs.writeSync(1, JSON.stringify({
      stats: reporter.stats,
      tests: tests.map(clean),
      failures: failures.map(clean),
      passes: passes.map(clean),
      pending: pending.map(clean)
    }, null, 2) + '\n');
  });
};
