// ==UserScript==
// @name         网易云切歌同步 v3
// @namespace    ghost-trigger
// @version      3.0
// @description  NetEase music sync for ghost-trigger
// @author       DS
// @match        https://music.163.com/*
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function() {
  "use strict";

  var SYNC_URL = "http://127.0.0.1:8766";
  var lastTitle = "";

  function log(msg) {
    console.log("[nsync] " + msg);
  }

  function post(path, data) {
    log("sending to " + path + ": " + JSON.stringify(data));
    GM_xmlhttpRequest({
      method: "POST",
      url: SYNC_URL + path,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify(data),
      onload: function(r) {
        log("posted OK: " + r.status);
      },
      onerror: function(e) {
        log("post ERROR");
      }
    });
  }

  function parseTitle(title) {
    if (!title || title === "网易云音乐" || title === "Cloud Music") return null;
    var cleaned = title.replace(/\s*[-—]\s*网易云音乐\s*$/, "").trim();
    if (!cleaned) return null;
    var sep = cleaned.indexOf(" - ");
    if (sep === -1) sep = cleaned.indexOf(" — ");
    if (sep > 0) {
      return {
        song_name: cleaned.substring(0, sep).trim(),
        artist: cleaned.substring(sep + 3).trim()
      };
    }
    return { song_name: cleaned, artist: "" };
  }

  function tick() {
    var title = document.title.trim();

    if (title.indexOf("网易云音乐") === 0 || title === "Cloud Music") {
      if (lastTitle !== "") {
        lastTitle = "";
        post("/stopped", {});
        log("stopped");
      }
      return;
    }

    if (title === lastTitle) return;
    lastTitle = title;

    var parsed = parseTitle(title);
    if (!parsed) {
      log("cant parse: " + title);
      return;
    }

    post("/nowplaying", {
      playing: true,
      song_name: parsed.song_name,
      artist: parsed.artist,
      source: "web"
    });
  }

  setInterval(tick, 5000);
  tick();
  log("ready");
})();
