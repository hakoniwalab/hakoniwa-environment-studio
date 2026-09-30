// Undo / redo history of a document kept as snapshots (strings, such as the
// Recipe as JSON). The browser has no undo for application state, so this is
// the one place it lives; tests/history_check.mjs covers it.
//
// The caller records a snapshot whenever editing settles (after a click, a
// finished drag or a released slider). Recording the same snapshot again is
// no step, so it is safe to record often; recording a new one after undoing
// drops the redo steps, as editors do.

export class History {
  // same(a, b): whether two snapshots are the same document (strings: ===).
  constructor(limit = 200, same = (a, b) => a === b) {
    this.limit = limit;
    this.same = same;
    this.reset(null);
  }

  // Start over from a snapshot (a document just opened): nothing to undo.
  reset(snapshot) {
    this.past = [];
    this.future = [];
    this.head = snapshot;
  }

  // The document as it is now; returns true when it became a new step.
  record(snapshot) {
    if (this.head === null) {
      this.head = snapshot;
      return false;
    }
    if (this.same(snapshot, this.head)) return false;
    this.past.push(this.head);
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
    this.head = snapshot;
    return true;
  }

  get canUndo() {
    return this.past.length > 0;
  }

  get canRedo() {
    return this.future.length > 0;
  }

  // The snapshot to go back to, or null when there is none.
  undo() {
    if (!this.canUndo) return null;
    this.future.push(this.head);
    this.head = this.past.pop();
    return this.head;
  }

  // The snapshot to go forward to again, or null when there is none.
  redo() {
    if (!this.canRedo) return null;
    this.past.push(this.head);
    this.head = this.future.pop();
    return this.head;
  }
}
