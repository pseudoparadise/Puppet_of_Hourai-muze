// ==UserScript==
// @name         网易云切歌同步 → ghost-trigger
// @namespace    ghost-trigger
// @version      1.0
// @description  在网易云网页切歌时自动同步到本地 ghost-trigger
// @author       DS老师
// @match        https://music.163.com/*
// @match        https://y.music.163.com/*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  "use strict";

  const SYNC_URL = "http://127.0.0.1:8766";
  let lastTitle = "";
  let stopped = false;
  let retries = 0;

  function post(path, data) {
    GM_xmlhttpRequest({
      method: "POST",
      url: SYNC_URL + path,
      headers: { "content-type": "application/json" },
      data: JSON.stringify(data),
      onerror: () => {
        retries++;
        if (retries <= 3) setTimeout(() => post(path, data), 3000);
      },
      onload: () => {
        retries = 0;
      },
    });
  }

  function getCover() {
    // 尝试从页面各个位置提取封面图
    const selectors = [
      ".g-mn .j-img",
      ".m-player .head img",
      ".play-cover img",
      ".u-cover img",
      'img[data-src*="music.126.net"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      const src = el?.src || el?.getAttribute("data-src") || "";
      if (src && src.includes("music.126.net")) {
        return src.replace(/\?param=\d+y\d+/, "?param=200y200");
      }
    }
    return "";
  }

  function parseTitle(title) {
    // "歌曲名 - 歌手名 - 网易云音乐" 或 "网易云音乐"
    if (!title || title === "网易云音乐") return null;

    // 移除后缀
    const cleaned = title
      .replace(/\s*-\s*网易云音乐\s*$/, "")
      .replace(/\s*—\s*网易云音乐\s*$/, "")
      .trim();

    if (!cleaned) return null;

    // 尝试 "歌手 - 歌曲" 或 "歌曲 - 歌手" 格式
    const parts = cleaned.split(/\s*[-—]\s*/);
    if (parts.length >= 2) {
      // 网易云标题格式通常是: 歌曲名 - 歌手名
      return {
        song_name: parts[0].trim(),
        artist: parts.slice(1).join(" / ").trim(),
      };
    }
    return { song_name: cleaned, artist: "" };
  }

  function tick() {
    const title = document.title.trim();

    // 没在播放
    if (title === "网易云音乐" || title === "Cloud Music") {
      if (!stopped) {
        stopped = true;
        lastTitle = "";
        post("/stopped", {});
        console.log("[netease-sync] 已停止");
      }
      return;
    }

    if (title === lastTitle) return;

    const parsed = parseTitle(title);
    if (!parsed) return;

    lastTitle = title;
    stopped = false;

    const cover = getCover();
    const data = {
      playing: true,
      song_name: parsed.song_name,
      artist: parsed.artist,
      album_pic: cover,
      source: "web",
    };

    post("/nowplaying", data);
    console.log("[netease-sync] ▶", parsed.song_name, "-", parsed.artist);
  }

  // 每 3 秒检查一次
  setInterval(tick, 3000);
  tick();

  console.log("[netease-sync] 已就位，开始监听切歌…");
})();
