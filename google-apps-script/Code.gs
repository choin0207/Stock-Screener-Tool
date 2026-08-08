/**
 * 台股選股雷達 — 登入驗證後台（Google Apps Script）
 *
 * 使用方式：在存放帳號密碼的 Google Sheet 中，點「擴充功能 → Apps Script」，
 * 把本檔全部內容貼上並儲存，再「部署 → 新增部署作業 → 網頁應用程式」
 * （執行身分：我；存取權：任何人），取得網址後貼到 docs/config.js 的 AUTH_URL。
 *
 * Google Sheet 格式（第一個工作表）：
 *   A欄=帳號, B欄=密碼（第 1 列為標題列，資料從第 2 列開始）
 */

function doPost(e) {
  var out = { ok: false };
  try {
    var req = JSON.parse(e.postData.contents);
    var user = String(req.user || '').trim();
    var pass = String(req.pass || '');
    if (!user || !pass) { out.error = '請輸入帳號與密碼'; return json_(out); }

    // 防暴力嘗試：同一帳號連錯 5 次，鎖 10 分鐘
    var cache = CacheService.getScriptCache();
    var key = 'fail_' + user;
    var fails = Number(cache.get(key) || 0);
    if (fails >= 5) { out.error = '嘗試次數過多，請 10 分鐘後再試'; return json_(out); }

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    var rows = sheet.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (String(rows[i][0]).trim() === user && String(rows[i][1]) === pass) {
        cache.remove(key);
        out.ok = true;
        out.user = user;
        out.exp = Date.now() + 7 * 24 * 3600 * 1000;   // 7 天內免重新登入
        return json_(out);
      }
    }
    cache.put(key, String(fails + 1), 600);
    out.error = '帳號或密碼錯誤';
  } catch (err) {
    out.error = '請求格式錯誤';
  }
  return json_(out);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}
