/* 条形码渲染 · Code 128B
 *
 * 纯前端实现：把条码号编码成 Code 128（Code Set B）并生成内联 SVG，
 * 不依赖任何第三方库，也不加载外部图片，符合站点 CSP（script-src 'self'）。
 *
 * 用法：
 *   Barcode.svg("BK0000001")                  // 返回 SVG 字符串
 *   Barcode.svg("BK0000001", { height: 64 })  // 自定义尺寸
 *   Barcode.isValid("BK0000001")              // 简单合法性判定
 */
(function (global) {
  "use strict";

  /* Code 128 码表：值 0–106，每个模式由 6 位（终止符 7 位）条空宽度组成 */
  var PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212",
    "221213", "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221",
    "223211", "221132", "221231", "213212", "223112", "312131", "311222", "321122", "321221",
    "312212", "322112", "322211", "212123", "212321", "232121", "111323", "131123", "131321",
    "112313", "132113", "132311", "211313", "231113", "231311", "112133", "112331", "132131",
    "113123", "113321", "133121", "313121", "211331", "231131", "213113", "213311", "213131",
    "311123", "311321", "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111", "111242",
    "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311",
    "113141", "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
  ];

  var START_B = 104;
  var STOP = 106;
  var QUIET_ZONE = 10; // 静区（模块数），两侧各留，保证可扫

  /** 启动自检：Code 128 每个模式宽度和必须为 11（终止符 13），防止码表被改坏 */
  (function selfCheck() {
    for (var i = 0; i < STOP; i++) {
      var sum = PATTERNS[i].split("").reduce(function (a, c) { return a + Number(c); }, 0);
      if (PATTERNS[i].length !== 6 || sum !== 11) {
        if (global.console) console.error("Barcode: 码表第 " + i + " 项非法");
        return;
      }
    }
    if (PATTERNS[STOP].length !== 7) {
      if (global.console) console.error("Barcode: 终止符非法");
    }
  })();

  function encode(text) {
    var values = [START_B];
    var checksum = START_B;
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (code < 32 || code > 126) {
        throw new Error("条码只支持可打印 ASCII 字符（字母、数字、常用符号）");
      }
      values.push(code - 32);
      checksum += (code - 32) * (i + 1);
    }
    values.push(checksum % 103);
    values.push(STOP);
    return values;
  }

  function modules(values) {
    var out = "";
    for (var i = 0; i < values.length; i++) out += PATTERNS[values[i]];
    return out;
  }

  function svg(text, options) {
    var raw = String(text == null ? "" : text).trim();
    if (!raw) return "";

    var opts = Object.assign(
      {
        height: 52,
        moduleWidth: 1.6,
        fontSize: 12,
        showText: true,
        color: "#111827",
        background: "#ffffff",
      },
      options || {}
    );

    var pattern = modules(encode(raw));
    var totalModules = QUIET_ZONE * 2;
    for (var i = 0; i < pattern.length; i++) totalModules += Number(pattern[i]);

    var width = totalModules * opts.moduleWidth;
    var height = opts.height + (opts.showText ? opts.fontSize + 10 : 0);
    var x = QUIET_ZONE * opts.moduleWidth;
    var bars = [];

    for (var j = 0; j < pattern.length; j++) {
      var barWidth = Number(pattern[j]) * opts.moduleWidth;
      if (j % 2 === 0) {
        bars.push('<rect x="' + x.toFixed(2) + '" y="0" width="' + barWidth.toFixed(2) +
          '" height="' + opts.height + '" />');
      }
      x += barWidth;
    }

    var label = opts.showText
      ? '<text x="' + (width / 2).toFixed(2) + '" y="' + (opts.height + opts.fontSize + 2) +
        '" text-anchor="middle" font-size="' + opts.fontSize +
        '" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" ' +
        'letter-spacing="1.5" fill="' + opts.color + '">' + escapeXml(raw) + "</text>"
      : "";

    return (
      '<svg class="barcode-svg" xmlns="http://www.w3.org/2000/svg" role="img" ' +
      'aria-label="馆藏条码 ' + escapeXml(raw) + '" ' +
      'viewBox="0 0 ' + width.toFixed(2) + " " + height + '" ' +
      'width="' + width.toFixed(2) + '" height="' + height + '" preserveAspectRatio="xMidYMid meet">' +
      '<rect x="0" y="0" width="' + width.toFixed(2) + '" height="' + height +
      '" fill="' + opts.background + '" />' +
      '<g fill="' + opts.color + '">' + bars.join("") + "</g>" +
      label +
      "</svg>"
    );
  }

  function escapeXml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function isValid(code) {
    var raw = String(code == null ? "" : code).trim();
    if (raw.length < 4) return false;
    for (var i = 0; i < raw.length; i++) {
      var code2 = raw.charCodeAt(i);
      if (code2 < 32 || code2 > 126) return false;
    }
    return true;
  }

  global.Barcode = { svg: svg, isValid: isValid, patterns: PATTERNS };
})(window);
