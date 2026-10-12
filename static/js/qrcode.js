/* 二维码渲染 · QR Code（Model 2）
 *
 * 纯前端实现：把文本编码成 QR 码矩阵并生成内联 SVG，
 * 不依赖任何第三方库、不加载外部图片，符合站点 CSP（script-src 'self'）。
 * 与 barcode.js 同一取舍——本项目只装了 Flask，前端所有编码都在本地算完。
 *
 * 支持范围（够「电子借书证」用即可，不做全版本）：
 *   * 8 位字节模式（UTF-8），版本 1–10，纠错等级 L/M/Q/H 自动选最小可用版本；
 *   * 自动掩码：按 ISO/IEC 18004 的四条罚分规则选最优掩码。
 *
 * 用法：
 *   QRCode.svg("8808001")                    // 返回 SVG 字符串
 *   QRCode.svg("8808001", { scale: 4 })
 *   QRCode.matrix("8808001")                 // 返回二维 0/1 数组（含静区）
 *   QRCode.selfTest()                        // 内置自检，返回 {ok, checks}
 */
(function (global, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (global) global.QRCode = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  /* ---------------------------------------------------------------- 常数表 */

  // 版本 → 纠错参数。每项 [块数, 该块总码字, 该块数据码字]（同版本可有多组块）
  var ECC_TABLE = {
    1: { L: [[1, 26, 19]], M: [[1, 26, 16]], Q: [[1, 26, 13]], H: [[1, 26, 9]] },
    2: { L: [[1, 44, 34]], M: [[1, 44, 28]], Q: [[1, 44, 22]], H: [[1, 44, 16]] },
    3: { L: [[1, 70, 55]], M: [[1, 70, 44]], Q: [[2, 35, 17]], H: [[2, 35, 13]] },
    4: { L: [[1, 100, 80]], M: [[2, 50, 32]], Q: [[2, 50, 24]], H: [[4, 25, 9]] },
    5: {
      L: [[1, 134, 108]], M: [[2, 67, 43]],
      Q: [[2, 33, 15], [2, 34, 16]], H: [[2, 33, 11], [2, 34, 12]],
    },
    6: { L: [[2, 86, 68]], M: [[4, 43, 27]], Q: [[4, 43, 19]], H: [[4, 43, 15]] },
    7: {
      L: [[2, 98, 78]], M: [[4, 49, 31]],
      Q: [[2, 32, 14], [4, 33, 15]], H: [[4, 39, 13], [1, 40, 14]],
    },
    8: {
      L: [[2, 121, 97]], M: [[2, 60, 38], [2, 61, 39]],
      Q: [[4, 40, 18], [2, 41, 19]], H: [[4, 40, 14], [2, 41, 15]],
    },
    9: {
      L: [[2, 146, 116]], M: [[3, 58, 36], [2, 59, 37]],
      Q: [[4, 36, 16], [4, 37, 17]], H: [[4, 36, 12], [4, 37, 13]],
    },
    10: {
      L: [[2, 86, 68], [2, 87, 69]], M: [[4, 69, 43], [1, 70, 44]],
      Q: [[6, 43, 19], [2, 44, 20]], H: [[6, 43, 15], [2, 44, 16]],
    },
  };

  // 版本 → 校正图形中心坐标（版本 1 没有校正图形）
  var ALIGNMENT_POS = {
    1: [],
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
  };

  // 纠错等级在格式信息里的 2 位取值
  var EC_BITS = { L: 1, M: 0, Q: 3, H: 2 };
  var LEVELS = ["L", "M", "Q", "H"];

  /* ---------------------------------------------------------------- GF(256) */

  var EXP = new Array(512);
  var LOG = new Array(256);
  (function initGalois() {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d; // 本原多项式 x^8+x^4+x^3+x^2+1
    }
    for (var j = 255; j < 512; j++) EXP[j] = EXP[j - 255];
  })();

  function gfMul(a, b) {
    if (a === 0 || b === 0) return 0;
    return EXP[LOG[a] + LOG[b]];
  }

  /** 生成多项式 (x-α^0)(x-α^1)...(x-α^(n-1))，系数从最高次到常数项 */
  function generatorPoly(n) {
    var poly = [1];
    for (var i = 0; i < n; i++) {
      var next = new Array(poly.length + 1).fill(0);
      for (var j = 0; j < poly.length; j++) {
        next[j] ^= poly[j];
        next[j + 1] ^= gfMul(poly[j], EXP[i]);
      }
      poly = next;
    }
    return poly;
  }

  /** 对 data 计算 n 个纠错码字 */
  function rsEncode(data, n) {
    var gen = generatorPoly(n);
    var buf = data.concat(new Array(n).fill(0));
    for (var i = 0; i < data.length; i++) {
      var coef = buf[i];
      if (coef === 0) continue;
      for (var j = 0; j < gen.length; j++) buf[i + j] ^= gfMul(gen[j], coef);
    }
    return buf.slice(data.length);
  }

  /* ---------------------------------------------------------------- 位缓冲 */

  function BitBuffer() {
    this.bits = [];
  }
  BitBuffer.prototype.put = function (value, length) {
    for (var i = length - 1; i >= 0; i--) this.bits.push((value >>> i) & 1);
  };
  BitBuffer.prototype.length = function () {
    return this.bits.length;
  };
  BitBuffer.prototype.toBytes = function () {
    var out = [];
    for (var i = 0; i < this.bits.length; i += 8) {
      var byte = 0;
      for (var j = 0; j < 8; j++) byte = (byte << 1) | (this.bits[i + j] || 0);
      out.push(byte);
    }
    return out;
  };

  /* ---------------------------------------------------------------- 文本 → 字节 */

  function utf8Bytes(text) {
    var str = String(text);
    var out = [];
    for (var i = 0; i < str.length; i++) {
      var code = str.charCodeAt(i);
      // 处理代理对
      if (code >= 0xd800 && code <= 0xdbff && i + 1 < str.length) {
        var low = str.charCodeAt(i + 1);
        if (low >= 0xdc00 && low <= 0xdfff) {
          code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
          i++;
        }
      }
      if (code < 0x80) out.push(code);
      else if (code < 0x800) out.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
      else if (code < 0x10000) {
        out.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
      } else {
        out.push(
          0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f),
          0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f)
        );
      }
    }
    return out;
  }

  /* ---------------------------------------------------------------- 码字生成 */

  function dataCapacity(version, level) {
    var total = 0;
    ECC_TABLE[version][level].forEach(function (blk) {
      total += blk[0] * blk[2];
    });
    return total;
  }

  function eccForVersion(version, level) {
    var ecc = [];
    ECC_TABLE[version][level].forEach(function (blk) {
      ecc.push(blk[1] - blk[2]);
    });
    return ecc;
  }

  /** 按版本 / 等级把数据码字切块、加纠错、交织成最终码字序列 */
  function buildCodewords(text, version, level) {
    var bytes = utf8Bytes(text);
    var capacity = dataCapacity(version, level);
    var buffer = new BitBuffer();
    buffer.put(4, 4); // 字节模式
    buffer.put(bytes.length, version < 10 ? 8 : 16);
    for (var i = 0; i < bytes.length; i++) buffer.put(bytes[i], 8);

    if (buffer.length() > capacity * 8) return null;

    // 终止符（最多 4 位，装不下就少写）
    var terminator = Math.min(4, capacity * 8 - buffer.length());
    buffer.put(0, terminator);
    // 补到字节边界
    while (buffer.length() % 8 !== 0) buffer.put(0, 1);

    var data = buffer.toBytes();
    // 交替填充字节
    var pad = [0xec, 0x11];
    var padIndex = 0;
    while (data.length < capacity) {
      data.push(pad[padIndex % 2]);
      padIndex++;
    }

    var blocks = ECC_TABLE[version][level];
    var eccCounts = eccForVersion(version, level);
    var dataBlocks = [];
    var eccBlocks = [];
    var offset = 0;
    for (var b = 0; b < blocks.length; b++) {
      var spec = blocks[b];
      for (var k = 0; k < spec[0]; k++) {
        var chunk = data.slice(offset, offset + spec[2]);
        offset += spec[2];
        dataBlocks.push(chunk);
        eccBlocks.push(rsEncode(chunk, spec[1] - spec[2]));
      }
    }
    void eccCounts;

    var result = [];
    var maxData = Math.max.apply(null, dataBlocks.map(function (b2) { return b2.length; }));
    for (var col = 0; col < maxData; col++) {
      for (var bi = 0; bi < dataBlocks.length; bi++) {
        if (col < dataBlocks[bi].length) result.push(dataBlocks[bi][col]);
      }
    }
    var maxEcc = Math.max.apply(null, eccBlocks.map(function (b3) { return b3.length; }));
    for (var ec = 0; ec < maxEcc; ec++) {
      for (var ei = 0; ei < eccBlocks.length; ei++) {
        if (ec < eccBlocks[ei].length) result.push(eccBlocks[ei][ec]);
      }
    }
    return result;
  }

  /* ---------------------------------------------------------------- 矩阵构造 */

  function makeMatrix(version, level, codewords) {
    var size = version * 4 + 17;
    var modules = [];
    var reserved = [];
    for (var r = 0; r < size; r++) {
      modules.push(new Array(size).fill(0));
      reserved.push(new Array(size).fill(false));
    }

    function set(row, col, dark) {
      modules[row][col] = dark ? 1 : 0;
      reserved[row][col] = true;
    }

    // 定位图形 + 分隔符
    function placeFinder(row, col) {
      for (var dr = -1; dr <= 7; dr++) {
        for (var dc = -1; dc <= 7; dc++) {
          var rr = row + dr;
          var cc = col + dc;
          if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
          var inRing = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
          var dark =
            inRing &&
            (dr === 0 || dr === 6 || dc === 0 || dc === 6 ||
              (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4));
          set(rr, cc, dark);
        }
      }
    }
    placeFinder(0, 0);
    placeFinder(0, size - 7);
    placeFinder(size - 7, 0);

    // 定时图形
    for (var t = 8; t < size - 8; t++) {
      var dark = t % 2 === 0;
      if (!reserved[6][t]) set(6, t, dark);
      if (!reserved[t][6]) set(t, 6, dark);
    }

    // 校正图形
    var positions = ALIGNMENT_POS[version] || [];
    for (var a = 0; a < positions.length; a++) {
      for (var b2 = 0; b2 < positions.length; b2++) {
        var cy = positions[a];
        var cx = positions[b2];
        if (reserved[cy][cx]) continue; // 与定位图形重叠的跳过
        for (var dy = -2; dy <= 2; dy++) {
          for (var dx = -2; dx <= 2; dx++) {
            var inner =
              Math.max(Math.abs(dy), Math.abs(dx)) !== 1; // 外圈与中心为深色
            set(cy + dy, cx + dx, inner);
          }
        }
      }
    }

    // 预留格式信息区（先占位，稍后写真实值）
    for (var i = 0; i <= 8; i++) {
      if (!reserved[8][i]) set(8, i, false);
      if (!reserved[i][8]) set(i, 8, false);
    }
    for (var j = 0; j < 8; j++) {
      if (!reserved[8][size - 1 - j]) set(8, size - 1 - j, false);
      if (!reserved[size - 1 - j][8]) set(size - 1 - j, 8, false);
    }
    set(size - 8, 8, true); // 固定深色模块

    // 版本信息（版本 ≥ 7）
    if (version >= 7) {
      var vbits = versionInfoBits(version);
      for (var k = 0; k < 18; k++) {
        var bit = (vbits >> k) & 1;
        var row1 = Math.floor(k / 3);
        var col1 = size - 11 + (k % 3);
        set(row1, col1, bit);
        set(col1, row1, bit);
      }
    }

    // 数据填充：从右下角起，两列一组蛇形向上
    var bitIndex = 0;
    var totalBits = codewords.length * 8;
    function nextBit() {
      if (bitIndex >= totalBits) return 0; // 剩余位补 0
      var byte = codewords[bitIndex >> 3];
      var bit = (byte >> (7 - (bitIndex & 7))) & 1;
      bitIndex++;
      return bit;
    }
    var upward = true;
    for (var col = size - 1; col > 0; col -= 2) {
      if (col === 6) col = 5; // 跳过定时列
      for (var step = 0; step < size; step++) {
        var row = upward ? size - 1 - step : step;
        for (var side = 0; side < 2; side++) {
          var target = col - side;
          if (reserved[row][target]) continue;
          modules[row][target] = nextBit();
        }
      }
      upward = !upward;
    }

    return { size: size, modules: modules, reserved: reserved, version: version, level: level };
  }

  /* ---------------------------------------------------------------- 格式 / 版本信息 */

  function bch(data, generator, genBits) {
    var value = data << genBits;
    var gBits = bitLength(generator);
    while (bitLength(value) >= gBits) {
      value ^= generator << (bitLength(value) - gBits);
    }
    return (data << genBits) | value;
  }

  function bitLength(value) {
    var len = 0;
    var v = value;
    while (v !== 0) {
      len++;
      v >>>= 1;
    }
    return len;
  }

  function formatInfoBits(level, mask) {
    var data = (EC_BITS[level] << 3) | mask;
    return (bch(data, 0x537, 10) ^ 0x5412) & 0x7fff;
  }

  function versionInfoBits(version) {
    return bch(version, 0x1f25, 12) & 0x3ffff;
  }

  /* ---------------------------------------------------------------- 掩码 */

  var MASK_FUNCS = [
    function (r, c) { return (r + c) % 2 === 0; },
    function (r) { return r % 2 === 0; },
    function (r, c) { return c % 3 === 0; },
    function (r, c) { return (r + c) % 3 === 0; },
    function (r, c) { return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0; },
    function (r, c) { return ((r * c) % 2) + ((r * c) % 3) === 0; },
    function (r, c) { return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0; },
    function (r, c) { return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0; },
  ];

  function applyMask(matrix, reserved, maskIndex) {
    var fn = MASK_FUNCS[maskIndex];
    for (var r = 0; r < matrix.size; r++) {
      for (var c = 0; c < matrix.size; c++) {
        if (reserved[r][c]) continue; // 只改数据区，功能图形不动
        if (fn(r, c)) matrix.modules[r][c] ^= 1;
      }
    }
  }

  function penalty(modules, size) {
    var score = 0;
    var r;
    var c;

    // 规则 1：连续同色 5 个起，每多 1 个 +1
    for (r = 0; r < size; r++) {
      var runRow = 1;
      for (c = 1; c < size; c++) {
        if (modules[r][c] === modules[r][c - 1]) {
          runRow++;
        } else {
          if (runRow >= 5) score += 3 + (runRow - 5);
          runRow = 1;
        }
      }
      if (runRow >= 5) score += 3 + (runRow - 5);
    }
    for (c = 0; c < size; c++) {
      var runCol = 1;
      for (r = 1; r < size; r++) {
        if (modules[r][c] === modules[r - 1][c]) {
          runCol++;
        } else {
          if (runCol >= 5) score += 3 + (runCol - 5);
          runCol = 1;
        }
      }
      if (runCol >= 5) score += 3 + (runCol - 5);
    }

    // 规则 2：2×2 同色块
    for (r = 0; r < size - 1; r++) {
      for (c = 0; c < size - 1; c++) {
        var v = modules[r][c];
        if (v === modules[r][c + 1] && v === modules[r + 1][c] && v === modules[r + 1][c + 1]) {
          score += 3;
        }
      }
    }

    // 规则 3：出现定位图形样式的 1:1:3:1:1 组合
    var p1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
    var p2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
    function matches(line, index, pattern) {
      for (var i = 0; i < pattern.length; i++) {
        if ((line[index + i] ? 1 : 0) !== pattern[i]) return false;
      }
      return true;
    }
    for (r = 0; r < size; r++) {
      var rowLine = modules[r];
      for (c = 0; c + 11 <= size; c++) {
        if (matches(rowLine, c, p1) || matches(rowLine, c, p2)) score += 40;
      }
    }
    for (c = 0; c < size; c++) {
      var colLine = [];
      for (r = 0; r < size; r++) colLine.push(modules[r][c]);
      for (r = 0; r + 11 <= size; r++) {
        if (matches(colLine, r, p1) || matches(colLine, r, p2)) score += 40;
      }
    }

    // 规则 4：深色比例偏离 50%
    var dark = 0;
    for (r = 0; r < size; r++) {
      for (c = 0; c < size; c++) dark += modules[r][c] ? 1 : 0;
    }
    var ratio = (dark * 100) / (size * size);
    score += Math.floor(Math.abs(ratio - 50) / 5) * 10;

    return score;
  }

  function placeFormatInfo(matrix, level, maskIndex) {
    var bits = formatInfoBits(level, maskIndex);
    var size = matrix.size;
    var m = matrix.modules;
    var b = function (i) {
      return (bits >> i) & 1;
    };

    // 副本 1（左上角）：列 8 自上而下放 bit0–5，行 8 自左向右放 bit14–9
    for (var i = 0; i <= 5; i++) {
      m[i][8] = b(i);
      m[8][i] = b(14 - i);
    }
    m[7][8] = b(6); // 跳过定时列
    m[8][7] = b(8);
    m[8][8] = b(7);

    // 副本 2（右上角 + 左下角）：行 8 自右向左放 bit0–7，列 8 自下而上放 bit14–7
    for (var j = 0; j <= 7; j++) {
      m[8][size - 1 - j] = b(j);
      m[size - 1 - j][8] = b(14 - j);
    }
    // 固定深色模块，位置与副本 2 的 bit7 重合，最后写以保证为深色
    m[size - 8][8] = 1;
  }

  /* ---------------------------------------------------------------- 公共 API */

  function chooseVersion(byteLength) {
    for (var v = 1; v <= 10; v++) {
      for (var li = 0; li < LEVELS.length; li++) {
        var level = LEVELS[li];
        var capacity = dataCapacity(v, level);
        var countBits = v < 10 ? 8 : 16;
        // 4 位模式 + 计数位 + 数据 + 终止符最多 4 位，换算成字节上取整
        var need = Math.ceil((4 + countBits + byteLength * 8 + 4) / 8);
        if (need <= capacity) return { version: v, level: level };
      }
    }
    return null;
  }

  /** 生成矩阵（含静区）。返回 { size, modules, version, level, scale } 或抛错。 */
  function matrix(text, options) {
    var opts = Object.assign({ level: "M", quietZone: 4 }, options || {});
    var raw = String(text == null ? "" : text);
    if (!raw) throw new Error("二维码内容不能为空");
    var bytes = utf8Bytes(raw);

    var chosen = null;
    if (opts.level) {
      for (var v = 1; v <= 10; v++) {
        var need = Math.ceil((4 + (v < 10 ? 8 : 16) + bytes.length * 8 + 4) / 8);
        if (need <= dataCapacity(v, opts.level)) {
          chosen = { version: v, level: opts.level };
          break;
        }
      }
    } else {
      chosen = chooseVersion(bytes.length);
    }
    if (!chosen) throw new Error("内容过长，超出本实现支持的版本范围（1–10）");

    var codewords = buildCodewords(raw, chosen.version, chosen.level);
    if (!codewords) throw new Error("内容过长，无法编码");

    var best = null;
    var bestScore = Infinity;
    var forced = opts.mask;
    var masks = forced === undefined || forced === null ? [0, 1, 2, 3, 4, 5, 6, 7] : [Number(forced)];
    for (var mi = 0; mi < masks.length; mi++) {
      var m = masks[mi];
      var candidate = makeMatrix(chosen.version, chosen.level, codewords);
      applyMask(candidate, candidate.reserved, m);
      placeFormatInfo(candidate, chosen.level, m);
      var score = penalty(candidate.modules, candidate.size);
      if (score < bestScore) {
        bestScore = score;
        best = { matrix: candidate, mask: m };
      }
    }

    var quiet = Number(opts.quietZone) || 0;
    var size = best.matrix.size + quiet * 2;
    var out = [];
    for (var r = 0; r < size; r++) out.push(new Array(size).fill(0));
    for (var rr = 0; rr < best.matrix.size; rr++) {
      for (var cc = 0; cc < best.matrix.size; cc++) {
        out[rr + quiet][cc + quiet] = best.matrix.modules[rr][cc];
      }
    }
    return {
      size: size,
      modules: out,
      version: chosen.version,
      level: chosen.level,
      mask: best.mask,
      quietZone: quiet,
    };
  }

  function escapeXml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /** 渲染成内联 SVG（用单个 path 画深色模块，体积小、放大不糊） */
  function svg(text, options) {
    var opts = Object.assign(
      { scale: 4, margin: 0, color: "#111827", background: "#ffffff", label: "" },
      options || {}
    );
    var data = matrix(text, { level: opts.level, quietZone: 0 });
    var scale = Number(opts.scale) || 4;
    var margin = opts.margin === undefined || opts.margin === null ? scale * 2 : Number(opts.margin);
    var dimension = data.size * scale + margin * 2;

    var path = [];
    for (var r = 0; r < data.size; r++) {
      for (var c = 0; c < data.size; c++) {
        if (!data.modules[r][c]) continue;
        var x = margin + c * scale;
        var y = margin + r * scale;
        path.push("M" + x + " " + y + "h" + scale + "v" + scale + "h-" + scale + "z");
      }
    }
    var label = opts.label || text;
    return (
      '<svg class="qrcode-svg" xmlns="http://www.w3.org/2000/svg" role="img" ' +
      'aria-label="二维码 ' + escapeXml(label) + '" ' +
      'viewBox="0 0 ' + dimension + " " + dimension + '" ' +
      'width="' + dimension + '" height="' + dimension + '" ' +
      'shape-rendering="crispEdges" preserveAspectRatio="xMidYMid meet">' +
      '<rect x="0" y="0" width="' + dimension + '" height="' + dimension +
      '" fill="' + opts.background + '"/>' +
      '<path fill="' + opts.color + '" d="' + path.join("") + '"/>' +
      "</svg>"
    );
  }

  /* ---------------------------------------------------------------- 自检 */

  /**
   * 内置自检：用 ISO/IEC 18004 的样例数据（"01234567"、版本 1-M）
   * 校验数据码字与纠错码字，并检查矩阵的功能图形是否落在该在的位置。
   * 返回 { ok, checks }，供测试脚本断言。
   */
  function selfTest() {
    var checks = [];

    // 样例：v1-M 的 16 个数据码字 + 10 个纠错码字（ISO/IEC 18004 附录示例）
    var expectedData = [
      0x10, 0x20, 0x0c, 0x56, 0x61, 0x80, 0xec, 0x11,
      0xec, 0x11, 0xec, 0x11, 0xec, 0x11, 0xec, 0x11,
    ];
    var expectedEcc = [
      0xa5, 0x24, 0xd4, 0xc1, 0xed, 0x36, 0xc7, 0x87, 0x2c, 0x55,
    ];

    var buffer = new BitBuffer();
    buffer.put(1, 4);          // 数字模式（样例用的是数字模式）
    buffer.put(8, 10);         // 数字模式在 v1 的计数位是 10 位
    buffer.put(0x1234567 >> 0, 0); // 占位，下面按三位一组手工写入
    buffer.bits = [];
    buffer.put(1, 4);
    buffer.put(8, 10);
    var digits = "01234567";
    for (var i = 0; i < digits.length; i += 3) {
      var group = digits.slice(i, i + 3);
      buffer.put(parseInt(group, 10), group.length * 3 + 1);
    }
    var term = Math.min(4, dataCapacity(1, "M") * 8 - buffer.length());
    buffer.put(0, term);
    while (buffer.length() % 8 !== 0) buffer.put(0, 1);
    var dataBytes = buffer.toBytes();
    var padIndex = 0;
    while (dataBytes.length < 16) {
      dataBytes.push(padIndex % 2 === 0 ? 0xec : 0x11);
      padIndex++;
    }
    checks.push({
      name: "数据码字（01234567 / 1-M）",
      pass: JSON.stringify(dataBytes) === JSON.stringify(expectedData),
      actual: dataBytes,
      expect: expectedData,
    });

    var ecc = rsEncode(dataBytes, 10);
    checks.push({
      name: "纠错码字（01234567 / 1-M）",
      pass: JSON.stringify(ecc) === JSON.stringify(expectedEcc),
      actual: ecc,
      expect: expectedEcc,
    });

    // 矩阵结构：三个定位图形中心必须是深色
    var m = makeMatrix(1, "M", buildCodewords("HELLO", 1, "M"));
    checks.push({
      name: "定位图形中心为深色",
      pass: m.modules[3][3] === 1 && m.modules[3][m.size - 4] === 1 && m.modules[m.size - 4][3] === 1,
    });
    checks.push({
      name: "定时图形交替",
      pass: m.modules[6][8] === 1 && m.modules[6][9] === 0 && m.modules[8][6] === 1 && m.modules[9][6] === 0,
    });
    checks.push({
      name: "固定深色模块",
      pass: m.modules[m.size - 8][8] === 1,
    });
    checks.push({
      name: "格式信息 BCH（M / 掩码 0）等于标准值",
      pass: formatInfoBits("M", 0) === 0x5412,
    });

    return { ok: checks.every(function (c) { return c.pass; }), checks: checks };
  }

  return {
    svg: svg,
    matrix: matrix,
    selfTest: selfTest,
    version: "1.0.0",
    _internal: {
      buildCodewords: buildCodewords,
      rsEncode: rsEncode,
      formatInfoBits: formatInfoBits,
      versionInfoBits: versionInfoBits,
      dataCapacity: dataCapacity,
    },
  };
});
