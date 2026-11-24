/* CA Mode: runs in <head> before the first paint (not deferred), so the sidebar starts in its final state.
   The rule is ui.js initialSidebarState(), which loads too late for this; tests/js/ui.test.js checks they agree. */
(function () {
  'use strict';
  var stored = null;
  try {
    stored = window.localStorage.getItem('ca-mode-sidebar');
  } catch (error) {
    stored = null;
  }
  // Closed up to 1280px; wider, the saved choice (open unless 'closed' was saved).
  if (window.innerWidth <= 1280 || stored === 'closed') document.documentElement.classList.add('sidebar-closed');
})();
