(function () {
  "use strict";

  var BASE = window.BASE_PATH || "";
  var LONG_PRESS_MS = 500;
  var CARD_WIDTH = 380;
  var SWIPE_MIN_PX = 60;

  var plates = [];
  var abortControllers = {};
  var lastVersion = {};
  var lastPage = {};     // track page to refetch object map on change
  var objectMaps = {};   // {plateName: {page: [obj, ...]}}
  var highlightEl = {};  // {plateName: div element}

  // ── Init ──────────────────────────────────────────────────────────────────

  function init() {
    fetch(BASE + "/api/plates")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        plates = data;
        if (plates.length === 0) {
          document.getElementById("empty-state").style.display = "";
          return;
        }

        var grid = document.getElementById("plate-grid");
        plates.forEach(function (p) {
          grid.appendChild(createCard(p));
          lastVersion[p.name] = 0;
          startLongPoll(p);
          fetchObjectMap(p.name);
        });
      })
      .catch(function () {
        setStatus("Error loading plates", true);
      });
  }

  // ── Card creation ─────────────────────────────────────────────────────────

  function createCard(plate) {
    var card = document.createElement("div");
    card.className = "plate-card";
    card.id = "card-" + plate.name;
    card.style.width = CARD_WIDTH + "px";

    var discovered = plate.discovered ? ' <span class="badge">auto</span>' : "";

    card.innerHTML =
      '<div class="card-header">' +
      '  <span class="plate-name">' + esc(plate.name) + discovered + "</span>" +
      '  <span class="plate-info">' +
      esc(plate.ip) + " &middot; " + plate.width + "x" + plate.height +
      "  </span>" +
      "</div>" +
      '<div class="screen-wrap" data-plate="' + esc(plate.name) + '" ' +
      '     data-w="' + plate.width + '" data-h="' + plate.height + '">' +
      '  <img id="ss-' + esc(plate.name) + '" ' +
      '       src="' + BASE + "/api/screenshot/" + encodeURIComponent(plate.name) + '" ' +
      '       alt="' + esc(plate.name) + ' screen" ' +
      '       draggable="false">' +
      '  <div class="touch-overlay"></div>' +
      '  <div class="highlight-box" id="hl-' + esc(plate.name) + '"></div>' +
      '  <div class="touch-dot" id="dot-' + esc(plate.name) + '"></div>' +
      "</div>" +
      '<div class="card-footer">' +
      '  <div class="plate-status">' +
      '    <span class="status-dot" id="sd-' + esc(plate.name) + '"></span>' +
      '    <span id="page-' + esc(plate.name) + '">Live</span>' +
      "  </div>" +
      '  <span class="update-info" id="ui-' + esc(plate.name) + '"></span>' +
      "</div>";

    var overlay = card.querySelector(".touch-overlay");
    attachTouchHandlers(overlay, plate);
    attachHoverHighlight(overlay, plate);

    return card;
  }

  // ── Long-polling loop ─────────────────────────────────────────────────────

  function startLongPoll(plate) {
    var errorCount = 0;

    function poll() {
      var controller = new AbortController();
      abortControllers[plate.name] = controller;

      var v = lastVersion[plate.name] || 0;
      fetch(BASE + "/api/wait/" + encodeURIComponent(plate.name) + "?v=" + v, {
        signal: controller.signal,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          errorCount = 0;

          // Update online/offline status (also controls dot color)
          updatePlateStatus(plate.name, data.online);

          if (data.v > lastVersion[plate.name]) {
            lastVersion[plate.name] = data.v;
            var img = document.getElementById("ss-" + plate.name);
            if (img) {
              img.src = BASE + "/api/screenshot/" +
                encodeURIComponent(plate.name) + "?v=" + data.v;
            }
            var infoEl = document.getElementById("ui-" + plate.name);
            if (infoEl) {
              var now = new Date();
              infoEl.textContent = "updated " +
                now.getHours().toString().padStart(2, "0") + ":" +
                now.getMinutes().toString().padStart(2, "0") + ":" +
                now.getSeconds().toString().padStart(2, "0");
            }
          }

          // Update page indicator + refetch object map on page change
          if (data.page !== undefined) {
            // Immediately update page in existing object map so
            // findObjectAt uses the correct page without waiting for refetch
            if (objectMaps[plate.name]) {
              objectMaps[plate.name].page = data.page;
            }

            // Show page + object count in footer
            var pageEl = document.getElementById("page-" + plate.name);
            if (pageEl) {
              var mapData = objectMaps[plate.name];
              var pgObjs = 0;
              if (mapData && mapData.objects) {
                pgObjs = (mapData.objects[String(data.page)] || []).length;
              }
              pageEl.textContent = "Page " + data.page + " (" + pgObjs + " obj)";
            }

            if (lastPage[plate.name] !== data.page) {
              lastPage[plate.name] = data.page;
              console.log("[page]", plate.name, "changed to", data.page);
              fetchObjectMap(plate.name);
            }
          }

          // Immediately re-poll
          poll();
        })
        .catch(function (err) {
          if (err.name === "AbortError") return;
          errorCount++;
          // This is a connection error to the mirror add-on, NOT plate offline.
          // Don't mark the plate as offline — only the backend LWT knows that.
          var dot = document.getElementById("sd-" + plate.name);
          if (dot) dot.className = "status-dot conn-lost";
          var infoEl = document.getElementById("ui-" + plate.name);
          if (infoEl) infoEl.textContent = "connection lost";
          // Back off on errors
          var delay = Math.min(2000 * Math.pow(2, errorCount), 30000);
          setTimeout(poll, delay);
        });
    }

    poll();
  }

  // ── Plate online/offline status ─────────────────────────────────────────

  var plateOnline = {};  // {plateName: bool}

  function updatePlateStatus(plateName, online) {
    var prev = plateOnline[plateName];
    plateOnline[plateName] = online;

    var dot = document.getElementById("sd-" + plateName);
    var card = document.getElementById("card-" + plateName);
    var pageEl = document.getElementById("page-" + plateName);

    if (online === false) {
      if (dot) dot.className = "status-dot offline";
      if (card) card.classList.add("plate-offline");
      if (pageEl) pageEl.textContent = "OFFLINE";
      // Only log transition
      if (prev !== false) {
        console.warn("[status]", plateName, "went OFFLINE");
      }
    } else {
      if (dot) dot.className = "status-dot";
      if (card) card.classList.remove("plate-offline");
      if (prev === false && pageEl) {
        pageEl.textContent = "Online";
        console.log("[status]", plateName, "back ONLINE");
      }
    }
  }

  // ── Object map for hover highlights ───────────────────────────────────────

  function fetchObjectMap(plateName) {
    fetch(BASE + "/api/objects/" + encodeURIComponent(plateName))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        objectMaps[plateName] = data;
        var pg = data.page || "?";
        var total = 0;
        if (data.objects) {
          for (var k in data.objects) {
            total += data.objects[k].length;
          }
        }
        console.log("[objmap]", plateName, "page=" + pg,
                    "total_objs=" + total, data.objects ? Object.keys(data.objects) : []);
      })
      .catch(function (err) {
        console.error("[objmap] fetch failed", plateName, err);
      });
  }

  function findObjectAt(plateName, x, y) {
    var data = objectMaps[plateName];
    if (!data || !data.objects) return null;

    var page = data.page || 1;
    // Check page 0 (overlay) then current page — last match wins (topmost)
    var pages = ["0", String(page)];
    var hit = null;
    for (var i = 0; i < pages.length; i++) {
      var objs = data.objects[pages[i]];
      if (!objs) continue;
      for (var j = 0; j < objs.length; j++) {
        var o = objs[j];
        if (x >= o.x && x < o.x + o.w && y >= o.y && y < o.y + o.h) {
          hit = o;  // keep overwriting — last match wins (topmost in render order)
        }
      }
    }
    return hit;
  }

  // ── Hover highlight ───────────────────────────────────────────────────────

  function attachHoverHighlight(overlay, plate) {
    var pendingRAF = null;
    overlay.addEventListener("mousemove", function (e) {
      if (pendingRAF) return;
      pendingRAF = requestAnimationFrame(function () {
        pendingRAF = null;
        var wrap = overlay.parentElement;
        var rect = wrap.getBoundingClientRect();
        var pw = parseInt(wrap.dataset.w, 10);
        var ph = parseInt(wrap.dataset.h, 10);
        var x = Math.round(((e.clientX - rect.left) / rect.width) * pw);
        var y = Math.round(((e.clientY - rect.top) / rect.height) * ph);

        var hl = document.getElementById("hl-" + plate.name);
        if (!hl) return;

        var obj = findObjectAt(plate.name, x, y);
        if (obj) {
          hl.style.left = ((obj.x / pw) * 100).toFixed(2) + "%";
          hl.style.top = ((obj.y / ph) * 100).toFixed(2) + "%";
          hl.style.width = ((obj.w / pw) * 100).toFixed(2) + "%";
          hl.style.height = ((obj.h / ph) * 100).toFixed(2) + "%";
          hl.classList.add("active");
          hl.title = "p" + obj.page + "b" + obj.id + " (" + obj.type + ")";
        } else {
          hl.classList.remove("active");
        }
      });
    });

    overlay.addEventListener("mouseleave", function () {
      var hl = document.getElementById("hl-" + plate.name);
      if (hl) hl.classList.remove("active");
    });
  }

  // ── Touch handlers (mouse + mobile touch) ─────────────────────────────────

  function attachTouchHandlers(overlay, plate) {
    var pressing = false;
    var longPressTimer = null;
    var startPos = null;
    var startTime = 0;

    function plateCoords(clientX, clientY) {
      var wrap = overlay.parentElement;
      var rect = wrap.getBoundingClientRect();
      var pw = parseInt(wrap.dataset.w, 10);
      var ph = parseInt(wrap.dataset.h, 10);
      return {
        x: Math.round(((clientX - rect.left) / rect.width) * pw),
        y: Math.round(((clientY - rect.top) / rect.height) * ph),
      };
    }

    function showDot(x, y) {
      var dot = document.getElementById("dot-" + plate.name);
      if (!dot) return;
      var wrap = overlay.parentElement;
      var pw = parseInt(wrap.dataset.w, 10);
      var ph = parseInt(wrap.dataset.h, 10);
      dot.style.left = ((x / pw) * 100).toFixed(1) + "%";
      dot.style.top = ((y / ph) * 100).toFixed(1) + "%";
      dot.classList.add("active");
    }

    function hideDot() {
      var dot = document.getElementById("dot-" + plate.name);
      if (dot) dot.classList.remove("active");
    }

    function sendTouch(x, y, state) {
      // Log what page/objects the frontend currently sees
      var mapData = objectMaps[plate.name];
      var curPage = mapData ? mapData.page : "none";
      var pgObjs = (mapData && mapData.objects) ? (mapData.objects[String(curPage)] || []).length : 0;
      var p0Objs = (mapData && mapData.objects) ? (mapData.objects["0"] || []).length : 0;
      console.log("[touch]", plate.name, "(" + x + "," + y + ")",
                  "frontendPage=" + curPage, "p0:" + p0Objs, "pg:" + pgObjs);

      fetch(BASE + "/api/touch/" + encodeURIComponent(plate.name), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: x, y: y, state: state }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          console.log("[touch-resp]", plate.name, data);
        })
        .catch(function () {});
    }

    function handleSwipe(startX, endX) {
      var dx = endX - startX;
      if (Math.abs(dx) < SWIPE_MIN_PX) return false;
      var dir = dx < 0 ? "next" : "prev";
      fetch(BASE + "/api/page/" + encodeURIComponent(plate.name), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir: dir }),
      })
        .then(function () {
          setTimeout(function () { fetchObjectMap(plate.name); }, 500);
        })
        .catch(function () {});
      return true;
    }

    // ── Mouse events ──

    overlay.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      pressing = true;
      startPos = plateCoords(e.clientX, e.clientY);
      startTime = Date.now();
      showDot(startPos.x, startPos.y);
    });

    overlay.addEventListener("mousemove", function (e) {
      if (!pressing) return;
      var pos = plateCoords(e.clientX, e.clientY);
      showDot(pos.x, pos.y);
    });

    overlay.addEventListener("mouseup", function (e) {
      if (!pressing) return;
      pressing = false;
      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }
      var endPos = plateCoords(e.clientX, e.clientY);

      // Check for swipe
      if (startPos && !handleSwipe(startPos.x, endPos.x)) {
        sendTouch(endPos.x, endPos.y, 0);
      }
      hideDot();
    });

    overlay.addEventListener("mouseleave", function () {
      if (!pressing) return;
      pressing = false;
      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }
      if (startPos) sendTouch(startPos.x, startPos.y, 0);
      hideDot();
    });

    overlay.addEventListener("contextmenu", function (e) {
      e.preventDefault();
    });

    // ── Touch events (mobile) ──

    var touchStartPos = null;
    var touchStartClient = null;

    overlay.addEventListener("touchstart", function (e) {
      e.preventDefault();
      var t = e.touches[0];
      touchStartPos = plateCoords(t.clientX, t.clientY);
      touchStartClient = { x: t.clientX, y: t.clientY };
      startTime = Date.now();
      showDot(touchStartPos.x, touchStartPos.y);
    }, { passive: false });

    overlay.addEventListener("touchmove", function (e) {
      e.preventDefault();
      var t = e.touches[0];
      var pos = plateCoords(t.clientX, t.clientY);
      showDot(pos.x, pos.y);
    }, { passive: false });

    overlay.addEventListener("touchend", function (e) {
      e.preventDefault();
      var endClient;
      if (e.changedTouches && e.changedTouches.length > 0) {
        endClient = {
          x: e.changedTouches[0].clientX,
          y: e.changedTouches[0].clientY,
        };
      } else {
        endClient = touchStartClient || { x: 0, y: 0 };
      }
      var endPos = plateCoords(endClient.x, endClient.y);

      if (touchStartPos && !handleSwipe(touchStartPos.x, endPos.x)) {
        sendTouch(endPos.x, endPos.y, 0);
      }

      touchStartPos = null;
      touchStartClient = null;
      hideDot();
    }, { passive: false });

    overlay.addEventListener("touchcancel", function () {
      touchStartPos = null;
      touchStartClient = null;
      hideDot();
    });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function setStatus(msg, isError) {
    var el = document.getElementById("status");
    if (el) {
      el.textContent = msg;
      el.className = isError ? "status-err" : "status-ok";
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", init);
})();
