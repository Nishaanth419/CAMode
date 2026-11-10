/* CA Mode: pure helpers for the chat and the profile dialog (loaded before app.js, exported for Node tests). */
(function (root) {
  'use strict';

  // Shown instead of raw server or browser error text (an exception message, a proxy's HTML page).
  const NETWORK_ERROR = 'Could not reach CA Mode. Check your connection and try again.';
  const GENERIC_ERROR = 'Something went wrong. Try again in a moment.';
  const STATUS_ERRORS = {
    400: 'CA Mode could not use that request. Check what you entered and try again.',
    401: 'Your session ended. Sign in again to continue.',
    403: 'You do not have access to that. Sign in again or unlock your history.',
    404: 'That could not be found. It may have been deleted.',
    408: 'The request took too long. Try again.',
    413: 'That is too large to send.',
    429: 'Too many requests right now. Wait a moment and try again.',
  };

  // An error body CA Mode wrote itself: JSON with a plain `error` sentence meant for the user.
  function apiError(body) {
    return body && typeof body === 'object' && typeof body.error === 'string' && body.error.trim() ? body.error.trim() : '';
  }

  // The message to show for a failed request: CA Mode's own error sentence, else a plain one for the status.
  function plainError(status, body) {
    const own = apiError(body);
    if (own) return own;
    if (!status) return NETWORK_ERROR;
    if (STATUS_ERRORS[status]) return STATUS_ERRORS[status];
    if (status >= 500) return 'CA Mode could not finish that. Try again in a moment.';
    return GENERIC_ERROR;
  }

  // The text to show for anything a request threw. CA Mode's own code throws plain `Error`s with sentences written
  // for the user; a failed fetch is a TypeError ("Failed to fetch", "Load failed"), and anything else (a JSON
  // SyntaxError, a bug) is internal text the user should not see.
  function errorText(error) {
    if (!error) return GENERIC_ERROR;
    if (error.name === 'TypeError' || error.name === 'AbortError') return NETWORK_ERROR;
    if (error.name === 'Error' && typeof error.message === 'string' && error.message.trim()) return error.message;
    return GENERIC_ERROR;
  }

  // Enter that finishes an IME composition (Japanese, Chinese, Korean, Indic keyboards) must not send.
  function isComposingEnter(event) {
    return Boolean(event && (event.isComposing || event.keyCode === 229));
  }

  const api = {
    NETWORK_ERROR, GENERIC_ERROR, plainError, errorText,
    isComposingEnter,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
