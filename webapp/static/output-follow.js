// Makes a streamed .output pane (see site.css) follow new output, like
// "tail -f" - but only while it's already scrolled to the bottom, so
// scrolling up to read something isn't yanked back down by the next line.
// Listens on document because the SSE-driven <pre> is swapped out for a
// fresh element on every run; these events bubble up from it.
(function () {
  // Pixel slack, since scrollTop is fractional on zoomed/scaled displays.
  const SLACK = 4;
  let following = true;

  document.addEventListener('htmx:sseBeforeMessage', function (e) {
    const el = e.target;
    following = el.scrollHeight - el.scrollTop - el.clientHeight <= SLACK;
  });

  document.addEventListener('htmx:sseMessage', function (e) {
    const el = e.target;
    if (!following) return;
    el.scrollTop = el.scrollHeight;
    // In case the swap settles after this event fires.
    requestAnimationFrame(function () {
      if (following) el.scrollTop = el.scrollHeight;
    });
  });
})();
