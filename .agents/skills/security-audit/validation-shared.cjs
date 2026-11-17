const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder } = require("node:util");

const SHARED_LIMITS = Object.freeze({
  inputBytes: 5 * 1024 * 1024,
  collectionItems: 1000,
  objectFields: 1000,
  nestingDepth: 64,
  preflightValues: 500000,
  validationErrors: 100,
});
const VISIBLE_CONTENT = /[^\p{White_Space}\p{Cc}\p{Cf}\p{Default_Ignorable_Code_Point}]/u;
const PATH_FORBIDDEN_CHARACTER = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}]/u;
const WINDOWS_RESERVED_COMPONENT = /^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(?:\.|$)/iu;
const UNSAFE_DIAGNOSTIC_CHARACTER = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}]/gu;
const UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });
const MAX_DIAGNOSTIC_STRING_LENGTH = 256;

class SafeInputError extends Error {}

function escapeUnsafeDiagnosticCharacters(value) {
  return String(value).replace(UNSAFE_DIAGNOSTIC_CHARACTER, (character) => {
    const codePoint = character.codePointAt(0);
    return codePoint <= 0xffff
      ? `\\u${codePoint.toString(16).padStart(4, "0")}`
      : `\\u{${codePoint.toString(16)}}`;
  });
}

function safeQuote(value) {
  let serialized;
  if (typeof value === "string") {
    const clipped = value.length > MAX_DIAGNOSTIC_STRING_LENGTH
      ? `${value.slice(0, MAX_DIAGNOSTIC_STRING_LENGTH)}...`
      : value;
    serialized = JSON.stringify(clipped);
  } else if (value === null || typeof value === "boolean") {
    serialized = String(value);
  } else if (typeof value === "number" && Number.isFinite(value)) {
    serialized = String(value);
  } else {
    serialized = `"<${Array.isArray(value) ? "array" : typeof value}>"`;
  }
  return escapeUnsafeDiagnosticCharacters(serialized);
}

function createErrorList() {
  const errors = [];
  Object.defineProperty(errors, "truncated", { value: false, writable: true });
  Object.defineProperty(errors, "push", {
    value(...messages) {
      const remaining = SHARED_LIMITS.validationErrors - this.length;
      if (messages.length > remaining) this.truncated = true;
      if (remaining > 0) {
        Array.prototype.push.apply(this, messages.slice(0, remaining).map(escapeUnsafeDiagnosticCharacters));
      }
      return this.length;
    },
  });
  return errors;
}

function hasValidUnicodeScalarValues(value) {
  let index = 0;
  while (index < value.length) {
    const first = value.charCodeAt(index++);
    if (first >= 0xd800 && first <= 0xdbff) {
      if (index >= value.length) return false;
      const second = value.charCodeAt(index++);
      if (second < 0xdc00 || second > 0xdfff) return false;
    } else if (first >= 0xdc00 && first <= 0xdfff) {
      return false;
    }
  }
  return true;
}

function hasVisibleProse(value) {
  return hasValidUnicodeScalarValues(value) && VISIBLE_CONTENT.test(value);
}

function isSafeRelativePath(value) {
  if (typeof value !== "string" || value.length === 0 || !hasValidUnicodeScalarValues(value) || value.trim() !== value || PATH_FORBIDDEN_CHARACTER.test(value) || value.includes("\\") || value.includes(":")) return false;
  if (path.posix.isAbsolute(value) || path.win32.isAbsolute(value) || /^[A-Za-z]:/.test(value) || value.startsWith("~")) return false;
  return value.split("/").every((segment) =>
    segment !== "" &&
    segment !== "." &&
    segment !== ".." &&
    !/[ .]$/u.test(segment) &&
    !WINDOWS_RESERVED_COMPONENT.test(segment));
}

class JsonStructureError extends Error {}

function preflightJsonText(contents, { topLevelItems = null } = {}) {
  const containers = [];
  let rootState = "value";
  let inString = false;
  let escaped = false;
  let totalValues = 0;

  function fail(message) {
    throw new JsonStructureError(`input ${message}`);
  }

  function currentContainer() {
    return containers[containers.length - 1];
  }

  function countValue() {
    totalValues++;
    if (totalValues > SHARED_LIMITS.preflightValues) {
      fail(`exceeds ${SHARED_LIMITS.preflightValues} total value limit`);
    }
  }

  function beginValue() {
    const container = currentContainer();
    if (!container) {
      if (rootState !== "value") fail("has malformed JSON structure");
      rootState = "end";
    } else if (container.type === "array") {
      if (container.state !== "firstValueOrEnd" && container.state !== "value") {
        fail("has malformed JSON array structure");
      }
      container.items++;
      const limit = container.topLevel && topLevelItems !== null
        ? topLevelItems
        : SHARED_LIMITS.collectionItems;
      if (container.items > limit) {
        fail(container.topLevel && topLevelItems !== null
          ? `exceeds ${limit} top-level unit limit`
          : `exceeds ${limit} item array limit`);
      }
      container.state = "commaOrEnd";
    } else {
      if (container.state !== "value") fail("has malformed JSON object structure");
      container.state = "commaOrEnd";
    }
    countValue();
  }

  function beginString() {
    const container = currentContainer();
    if (container && container.type === "object" &&
        (container.state === "firstKeyOrEnd" || container.state === "key")) {
      container.fields++;
      if (container.fields > SHARED_LIMITS.objectFields) {
        fail(`exceeds ${SHARED_LIMITS.objectFields} field object limit`);
      }
      container.state = "colon";
    } else {
      beginValue();
    }
    inString = true;
  }

  function beginContainer(type) {
    beginValue();
    if (containers.length >= SHARED_LIMITS.nestingDepth) {
      fail(`exceeds nesting depth limit ${SHARED_LIMITS.nestingDepth}`);
    }
    containers.push(type === "array"
      ? { type, state: "firstValueOrEnd", items: 0, topLevel: containers.length === 0 }
      : { type, state: "firstKeyOrEnd", fields: 0 });
  }

  function closeContainer(type) {
    const container = currentContainer();
    if (!container || container.type !== type) fail("has mismatched JSON containers");
    const canClose = type === "array"
      ? container.state === "firstValueOrEnd" || container.state === "commaOrEnd"
      : container.state === "firstKeyOrEnd" || container.state === "commaOrEnd";
    if (!canClose) fail(`has malformed JSON ${type} structure`);
    containers.pop();
  }

  function isWhitespace(character) {
    return character === " " || character === "\t" || character === "\r" || character === "\n";
  }

  function isTokenDelimiter(character) {
    return isWhitespace(character) || character === "," || character === ":" ||
      character === "[" || character === "]" || character === "{" ||
      character === "}" || character === "\"";
  }

  for (let index = 0; index < contents.length; index++) {
    const character = contents[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === "\"") {
        inString = false;
      }
      continue;
    }
    if (isWhitespace(character)) continue;

    if (character === "\"") {
      beginString();
    } else if (character === "[") {
      beginContainer("array");
    } else if (character === "{") {
      beginContainer("object");
    } else if (character === "]") {
      closeContainer("array");
    } else if (character === "}") {
      closeContainer("object");
    } else if (character === ":") {
      const container = currentContainer();
      if (!container || container.type !== "object" || container.state !== "colon") {
        fail("has malformed JSON object structure");
      }
      container.state = "value";
    } else if (character === ",") {
      const container = currentContainer();
      if (!container || container.state !== "commaOrEnd") fail("has malformed JSON collection structure");
      container.state = container.type === "array" ? "value" : "key";
    } else {
      beginValue();
      while (index + 1 < contents.length && !isTokenDelimiter(contents[index + 1])) index++;
    }
  }

  if (inString) fail("has an unterminated JSON string");
  if (containers.length > 0) fail("has truncated JSON structure");
  if (rootState !== "end") fail("has no JSON value");
}

function appendErrors(target, source) {
  target.push(...source);
  target.truncated ||= Boolean(source.truncated);
  return target;
}

function preflightDocument(root, { topLevelItems = null, location = "$" } = {}) {
  const errors = createErrorList();
  const stack = [{ value: root, depth: 0, location, topLevel: true }];
  const seen = new WeakSet();
  let visited = 0;

  function fail(message) {
    errors.push(message);
    return errors;
  }

  function countScalar(parentLocation) {
    visited++;
    if (visited > SHARED_LIMITS.preflightValues) {
      return fail(`${parentLocation}: exceeds ${SHARED_LIMITS.preflightValues} total value limit`);
    }
    return null;
  }

  while (stack.length > 0) {
    const current = stack.pop();
    const limitError = countScalar(current.location);
    if (limitError) return limitError;
    if (current.value === null || typeof current.value !== "object") continue;
    if (current.depth >= SHARED_LIMITS.nestingDepth) {
      return fail(`${current.location}: exceeds nesting depth limit ${SHARED_LIMITS.nestingDepth}`);
    }
    if (seen.has(current.value)) {
      return fail(`${current.location}: input must not contain repeated or cyclic object references`);
    }
    seen.add(current.value);

    if (Array.isArray(current.value)) {
      const limit = current.topLevel && topLevelItems !== null
        ? topLevelItems
        : SHARED_LIMITS.collectionItems;
      if (current.value.length > limit) {
        return fail(`${current.location}: exceeds ${limit} item array limit`);
      }
      for (let index = current.value.length - 1; index >= 0; index--) {
        const child = current.value[index];
        if (child === null || typeof child !== "object") {
          visited++;
          if (visited > SHARED_LIMITS.preflightValues) {
            return fail(`${current.location}[${index}]: exceeds ${SHARED_LIMITS.preflightValues} total value limit`);
          }
          continue;
        }
        stack.push({
          value: child,
          location: `${current.location}[${index}]`,
          depth: current.depth + 1,
          topLevel: false,
        });
      }
      continue;
    }

    const keys = Object.keys(current.value);
    if (keys.length > SHARED_LIMITS.objectFields) {
      return fail(`${current.location}: exceeds ${SHARED_LIMITS.objectFields} field object limit`);
    }
    for (let index = keys.length - 1; index >= 0; index--) {
      const key = keys[index];
      const child = current.value[key];
      if (child === null || typeof child !== "object") {
        visited++;
        if (visited > SHARED_LIMITS.preflightValues) {
          return fail(`${current.location}{${safeQuote(key)}}: exceeds ${SHARED_LIMITS.preflightValues} total value limit`);
        }
        continue;
      }
      stack.push({
        value: child,
        location: `${current.location}{${safeQuote(key)}}`,
        depth: current.depth + 1,
        topLevel: false,
      });
    }
  }

  return errors;
}

function readFileWithinLimit(file) {
  const noFollow = fs.constants.O_NOFOLLOW;
  const nonBlock = fs.constants.O_NONBLOCK;
  if (!Number.isInteger(noFollow) || noFollow === 0 || !Number.isInteger(nonBlock) || nonBlock === 0) {
    // Node exposes no race-safe fallback on these platforms, so reject all inputs.
    throw new SafeInputError("OS no-follow and nonblocking input protection is unavailable");
  }

  let descriptor;
  try {
    descriptor = fs.openSync(file, fs.constants.O_RDONLY | noFollow | nonBlock);
  } catch (error) {
    if (error && (error.code === "ELOOP" || error.code === "EMLINK")) {
      throw new SafeInputError("input must not be a symlink");
    }
    throw error;
  }
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile()) throw new SafeInputError("input must be a regular file");
    if (stat.size > SHARED_LIMITS.inputBytes) {
      throw new SafeInputError(`input exceeds ${SHARED_LIMITS.inputBytes} byte limit`);
    }

    const chunks = [];
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let bytesRead = 0;
    while (true) {
      const count = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      bytesRead += count;
      if (bytesRead > SHARED_LIMITS.inputBytes) {
        throw new SafeInputError(`input exceeds ${SHARED_LIMITS.inputBytes} byte limit`);
      }
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    try {
      return UTF8_DECODER.decode(Buffer.concat(chunks, bytesRead));
    } catch {
      throw new SafeInputError("input is not valid UTF-8");
    }
  } finally {
    fs.closeSync(descriptor);
  }
}

module.exports = {
  PATH_FORBIDDEN_CHARACTER,
  SHARED_LIMITS,
  JsonStructureError,
  SafeInputError,
  UNSAFE_DIAGNOSTIC_CHARACTER,
  VISIBLE_CONTENT,
  WINDOWS_RESERVED_COMPONENT,
  appendErrors,
  createErrorList,
  escapeUnsafeDiagnosticCharacters,
  hasValidUnicodeScalarValues,
  hasVisibleProse,
  isSafeRelativePath,
  preflightDocument,
  preflightJsonText,
  readFileWithinLimit,
  safeQuote,
};
