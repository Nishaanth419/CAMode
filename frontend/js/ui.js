/* CA Mode: pure helpers for the chat and the profile dialog (loaded before app.js, exported for Node tests). */
(function (root) {
  'use strict';

  // Enter that finishes an IME composition (Japanese, Chinese, Korean, Indic keyboards) must not send.
  function isComposingEnter(event) {
    return Boolean(event && (event.isComposing || event.keyCode === 229));
  }

  const api = {
    isComposingEnter,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
