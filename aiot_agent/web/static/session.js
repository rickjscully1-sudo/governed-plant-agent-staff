/* Per-visitor demo session.
   The server issues a random session id (cookie + X-Demo-Session header).
   We also keep it in sessionStorage and send it back as a header, so the demo
   stays isolated per visitor even inside an iframe where cookies are blocked.
   Your runs, approvals and audit trail are never shared with other visitors. */

(function () {
  "use strict";
  var KEY = "aiot_demo_sid";
  var HEADER = "X-Demo-Session";
  var memorySid = null;

  function readSid() {
    try { return window.sessionStorage.getItem(KEY) || memorySid; } catch (e) { return memorySid; }
  }
  function writeSid(sid) {
    memorySid = sid;
    try { window.sessionStorage.setItem(KEY, sid); } catch (e) { /* storage blocked: keep in memory */ }
  }

  window.demoFetch = function (url, opts) {
    var o = opts ? Object.assign({}, opts) : {};
    var headers = Object.assign({}, o.headers || {});
    var sid = readSid();
    if (sid) headers[HEADER] = sid;
    o.headers = headers;
    o.credentials = "same-origin";
    return fetch(url, o).then(function (res) {
      var next = res.headers.get(HEADER);
      if (next && /^[A-Za-z0-9_-]{32,64}$/.test(next)) writeSid(next);
      return res;
    });
  };
})();
