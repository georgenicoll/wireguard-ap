// Presence of a fetch handler is what makes Chrome/Edge treat the site as
// installable - this app shows live status, so it deliberately does no
// caching and just passes every request straight through to the network.
self.addEventListener("fetch", () => {});
