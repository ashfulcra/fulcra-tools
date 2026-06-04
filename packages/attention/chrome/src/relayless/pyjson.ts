// chrome/src/relayless/pyjson.ts
//
// CROSS-LANGUAGE CONTRACT. The relayless extension POSTs records to
// /ingest/v1/record/batch whose inner `data` string MUST be byte-identical
// to what the daemon's Python pipeline emits via
//   json.dumps(data, sort_keys=True)
// (fulcra_common/wire.py:build_record). Fulcra dedupes on the record's
// source_id, but byte parity keeps the stored payloads identical whether an
// event arrived via the daemon or relaylessly. Python's default dumps is:
//   - ensure_ascii=True       → every non-ASCII codepoint becomes \uXXXX
//                               (astral chars become a UTF-16 surrogate pair,
//                               matching JS string semantics)
//   - separators=(", ", ": ") → item/key separators carry a trailing space
//                               (this is the indent=None default)
//   - sort_keys=True          → object keys sorted lexicographically by their
//                               UTF-16 code units (same order Python's str
//                               comparison yields for the BMP keys we emit)
//
// JSON.stringify diverges on both the separators (no spaces) and ASCII
// escaping (it leaves non-ASCII raw), so we hand-roll the encoder.

function encodeString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    const ch = s[i];
    switch (ch) {
      case '"':
        out += '\\"';
        break;
      case "\\":
        out += "\\\\";
        break;
      case "\b":
        out += "\\b";
        break;
      case "\f":
        out += "\\f";
        break;
      case "\n":
        out += "\\n";
        break;
      case "\r":
        out += "\\r";
        break;
      case "\t":
        out += "\\t";
        break;
      default:
        if (code < 0x20 || code > 0x7e) {
          // Control chars and everything non-ASCII → \uXXXX. charCodeAt
          // yields UTF-16 code units, so astral chars already arrive as
          // their surrogate pair (e.g. 😀 → 😀), matching
          // Python's ensure_ascii surrogate-pair output.
          out += "\\u" + code.toString(16).padStart(4, "0");
        } else {
          out += ch;
        }
    }
  }
  return out + '"';
}

function encodeValue(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return encodeString(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) {
      // Python json.dumps emits NaN/Infinity bare; we never produce these
      // in the wire payload, so treat as an error rather than silently
      // diverging.
      throw new Error(`pyJsonStringify: non-finite number ${v}`);
    }
    // Integers print without a decimal point in both languages. Floats are
    // not part of the attention wire shape (duration_seconds is an int), so
    // we rely on JS's default number formatting for the integer case.
    return String(v);
  }
  if (Array.isArray(v)) {
    return "[" + v.map(encodeValue).join(", ") + "]";
  }
  if (typeof v === "object") {
    const obj = v as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    const parts = keys.map((k) => encodeString(k) + ": " + encodeValue(obj[k]));
    return "{" + parts.join(", ") + "}";
  }
  throw new Error(`pyJsonStringify: unsupported type ${typeof v}`);
}

/**
 * Serialize `value` exactly the way Python's
 * `json.dumps(value, sort_keys=True)` does (default ensure_ascii, default
 * separators). Used to build the inner `data` string of an ingest record so
 * it is byte-for-byte identical to the daemon's output.
 */
export function pyJsonStringify(value: unknown): string {
  return encodeValue(value);
}
