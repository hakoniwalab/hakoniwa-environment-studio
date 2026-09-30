// Checks web/history.js (run by tests/test_env_studio.py when Node.js is present).
import assert from "node:assert/strict";
import { History } from "../web/history.js";

// Nothing to undo right after opening a document.
let history = new History();
history.reset("a");
assert.equal(history.canUndo, false);
assert.equal(history.undo(), null);
assert.equal(history.redo(), null);

// Each different snapshot is one step; the same one again is none.
assert.equal(history.record("a"), false);
assert.equal(history.record("b"), true);
assert.equal(history.record("b"), false);
assert.equal(history.record("c"), true);
assert.equal(history.undo(), "b");
assert.equal(history.undo(), "a");
assert.equal(history.undo(), null); // nothing before the opened document
assert.equal(history.head, "a");
assert.equal(history.redo(), "b");
assert.equal(history.redo(), "c");
assert.equal(history.redo(), null);

// Recording after undoing drops the redo steps.
history.undo(); // back to b
assert.equal(history.record("d"), true);
assert.equal(history.canRedo, false);
assert.equal(history.undo(), "b");
assert.equal(history.undo(), "a");

// Recording what undo returned (the caller re-renders it) is no step and
// keeps the redo steps.
history = new History();
history.reset("a");
history.record("b");
assert.equal(history.record(history.undo()), false);
assert.equal(history.canRedo, true);

// Before reset, the first record only sets where history starts.
history = new History();
assert.equal(history.record("x"), false);
assert.equal(history.canUndo, false);

// Only the newest steps are kept.
history = new History(3);
history.reset("0");
for (const snapshot of ["1", "2", "3", "4", "5"]) history.record(snapshot);
assert.deepEqual([history.undo(), history.undo(), history.undo(), history.undo()], ["4", "3", "2", null]);

// Snapshots other than strings compare with the given same(): an equal copy is no step.
history = new History(10, (a, b) => a === b || (a !== null && b !== null && a.join("|") === b.join("|")));
history.reset(["a", "b"]);
assert.equal(history.record(["a", "b"]), false);
assert.equal(history.record(["a", "c"]), true);
assert.deepEqual(history.undo(), ["a", "b"]);

console.log("history OK");
