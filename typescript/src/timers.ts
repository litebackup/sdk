/** The longest delay Node's timers take; a longer one fires at once. */
export const maximumTimerMillis = 2 ** 31 - 1;

/**
 * Whether a client-side deadline of millis can be timed. A longer one is not
 * set: the daemon's own timeout, which the call also sends, ends the call.
 */
export function timeable(millis: number | undefined): millis is number {
  return millis !== undefined && millis > 0 && millis <= maximumTimerMillis;
}
