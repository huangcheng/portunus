#!/usr/bin/env node

/**
 * Validates coverage-ledger.json and its canonical coverage IDs.
 * Usage: node validate-coverage-ledger.cjs <path-to-coverage-ledger.json>
 */

const {
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
  hasVisibleProse,
  isSafeRelativePath,
  preflightDocument: preflightDocumentShared,
  preflightJsonText: preflightJsonTextShared,
  readFileWithinLimit,
  safeQuote,
} = require("./validation-shared.cjs");

// Conservative bounds apply before JSON.parse and again to the parsed document.
const LIMITS = Object.freeze({
  ...SHARED_LIMITS,
  units: 10000,
});
const MAX_UNITS = LIMITS.units;
const MAX_LIST_ITEMS = LIMITS.collectionItems;
const MAX_TEXT_LENGTH = 4096;
const REQUIRED_FIELDS = [
  "coverage_id",
  "canonical_refs",
  "surface",
  "boundary",
  "subsystem",
  "attack_class",
  "starting_paths",
  "ordinary_attack_class_block",
  "selected_companion_blocks",
  "excluded_blocks",
  "prior_status",
  "attempts",
  "wave",
  "status",
  "agent_id",
  "reviewed_paths",
  "local_checks",
  "result_fingerprints",
  "unresolved",
];
const REF_FIELDS = ["surface", "boundary", "subsystem", "attack_class"];
const STATUSES = new Set([
  "planned",
  "not_applicable",
  "out_of_scope",
  "in_progress",
  "covered",
  "candidate",
  "blocked",
  "deferred",
]);
const ATTEMPT_STATUSES = new Set(["covered", "candidate", "blocked"]);
const ATTEMPT_FIELDS = [
  "wave",
  "status",
  "agent_id",
  "reviewed_paths",
  "local_checks",
  "result_fingerprints",
  "unresolved",
  "reassignment_reason",
];
const PRIOR_STATUSES = new Set([
  "new",
  "prior_confirmed_same_source",
  "prior_confirmed_changed_source",
  "prior_needs_validation",
  "prior_deferred",
  "prior_blocked",
  "prior_out_of_scope",
  "prior_covered_same_source",
  "prior_covered_changed_source",
  "prior_rejected_claim_changed",
  "none",
]);
const FINGERPRINT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;
const AGENT_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const WINDOWS_RESERVED_AGENT_ID = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/;
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function preflightJsonText(contents) {
  preflightJsonTextShared(contents, { topLevelItems: MAX_UNITS });
}

function preflightDocument(root) {
  return preflightDocumentShared(root, { topLevelItems: MAX_UNITS });
}

function isVisibleText(value, maxLength = MAX_TEXT_LENGTH) {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= maxLength &&
    value.trim() === value &&
    hasVisibleProse(value);
}

function isCanonicalRef(value) {
  return isVisibleText(value, 1024) &&
    !PATH_FORBIDDEN_CHARACTER.test(value) &&
    value.normalize("NFC") === value;
}

function encodeCanonicalRef(value) {
  if (!isCanonicalRef(value)) throw new TypeError("invalid canonical reference");
  let encoded = "";
  for (const byte of Buffer.from(value, "utf8")) {
    const unreserved =
      (byte >= 0x41 && byte <= 0x5a) ||
      (byte >= 0x61 && byte <= 0x7a) ||
      (byte >= 0x30 && byte <= 0x39) ||
      byte === 0x2d || byte === 0x2e || byte === 0x5f || byte === 0x7e;
    encoded += unreserved ? String.fromCharCode(byte) : `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return encoded;
}

function canonicalCoverageId(refs) {
  if (!isObject(refs)) throw new TypeError("canonical_refs must be an object");
  const fields = hasOwn(refs, "lifecycle") ? [...REF_FIELDS, "lifecycle"] : REF_FIELDS;
  if (Object.keys(refs).length !== fields.length || fields.some((field) => !hasOwn(refs, field))) {
    throw new TypeError("canonical_refs has missing or unexpected fields");
  }
  return fields.map((field) => encodeCanonicalRef(refs[field])).join("::");
}

function isSafeAgentId(value) {
  return typeof value === "string" &&
    AGENT_ID_PATTERN.test(value) &&
    !WINDOWS_RESERVED_AGENT_ID.test(value);
}

function isOwnedArtifactPath(value, agentId) {
  if (!isSafeAgentId(agentId) || !isSafeRelativePath(value)) return false;
  const prefix = `agents/${agentId}/artifacts/`;
  return value.startsWith(prefix) && value.length > prefix.length;
}

function validateStringArray(value, location, errors, options = {}) {
  const { allowEmpty = true, fingerprint = false, pathValue = false } = options;
  if (!Array.isArray(value)) {
    errors.push(`${location}: expected array`);
    return;
  }
  if (!allowEmpty && value.length === 0) errors.push(`${location}: must not be empty`);
  if (value.length > MAX_LIST_ITEMS) errors.push(`${location}: exceeds ${MAX_LIST_ITEMS} entries`);
  const seen = new Set();
  value.slice(0, MAX_LIST_ITEMS).forEach((entry, index) => {
    const entryLocation = `${location}[${index}]`;
    const valid = pathValue ? isSafeRelativePath(entry) : isVisibleText(entry);
    if (!valid) errors.push(`${entryLocation}: invalid ${pathValue ? "repository-relative path" : "text"}`);
    if (fingerprint && typeof entry === "string" && !FINGERPRINT_PATTERN.test(entry)) {
      errors.push(`${entryLocation}: invalid fingerprint`);
    }
    if (typeof entry === "string" && seen.has(entry)) errors.push(`${entryLocation}: duplicate entry`);
    if (typeof entry === "string") seen.add(entry);
  });
}

function validateChecks(value, location, errors) {
  if (!Array.isArray(value)) {
    errors.push(`${location}: expected array`);
    return;
  }
  if (value.length > MAX_LIST_ITEMS) errors.push(`${location}: exceeds ${MAX_LIST_ITEMS} entries`);
  value.slice(0, MAX_LIST_ITEMS).forEach((check, index) => {
    const base = `${location}[${index}]`;
    if (!isObject(check)) {
      errors.push(`${base}: expected object`);
      return;
    }
    for (const field of ["agent_id", "reviewed_paths", "invariant", "method", "result", "artifact"]) {
      if (!hasOwn(check, field)) errors.push(`${base}: missing required field ${safeQuote(field)}`);
    }
    if (!isSafeAgentId(check.agent_id)) errors.push(`${base}.agent_id: expected a canonical lowercase agent ID`);
    validateStringArray(check.reviewed_paths, `${base}.reviewed_paths`, errors, { allowEmpty: false, pathValue: true });
    if (!isVisibleText(check.invariant)) errors.push(`${base}.invariant: invalid text`);
    if (check.method !== "source" && check.method !== "local") errors.push(`${base}.method: expected "source" or "local"`);
    if (!isVisibleText(check.result)) errors.push(`${base}.result: invalid text`);
    if (check.method === "source" && check.artifact !== null) {
      errors.push(`${base}.artifact: source-only check must use null`);
    } else if (check.method === "local") {
      if (!isOwnedArtifactPath(check.artifact, check.agent_id)) {
        errors.push(`${base}.artifact: local check requires an artifact owned by agent ${safeQuote(isSafeAgentId(check.agent_id) ? check.agent_id : "<agent-id>")}`);
      }
    } else if (check.artifact !== null && !isSafeRelativePath(check.artifact)) {
      errors.push(`${base}.artifact: expected null or a safe output-relative path`);
    }
  });
}

function validateReviewedPathOwnership(unit, base, errors) {
  if (!Array.isArray(unit.reviewed_paths) || !Array.isArray(unit.local_checks)) return;
  const aggregatePaths = new Set(unit.reviewed_paths.filter((value) => typeof value === "string"));
  const ownedPaths = new Set();
  for (const check of unit.local_checks) {
    if (!isObject(check) || !Array.isArray(check.reviewed_paths)) continue;
    for (const reviewedPath of check.reviewed_paths) {
      if (typeof reviewedPath === "string") ownedPaths.add(reviewedPath);
    }
  }
  for (const reviewedPath of aggregatePaths) {
    if (!ownedPaths.has(reviewedPath)) errors.push(`${base}.reviewed_paths: ${safeQuote(reviewedPath)} has no check owner`);
  }
  for (const reviewedPath of ownedPaths) {
    if (!aggregatePaths.has(reviewedPath)) errors.push(`${base}.local_checks: owned path ${safeQuote(reviewedPath)} is absent from aggregate reviewed_paths`);
  }
}

function validateExcludedBlocks(value, location, errors) {
  if (!Array.isArray(value)) {
    errors.push(`${location}: expected array`);
    return;
  }
  if (value.length > MAX_LIST_ITEMS) errors.push(`${location}: exceeds ${MAX_LIST_ITEMS} entries`);
  const seen = new Set();
  value.slice(0, MAX_LIST_ITEMS).forEach((entry, index) => {
    const base = `${location}[${index}]`;
    if (!isObject(entry)) {
      errors.push(`${base}: expected object`);
      return;
    }
    if (!isVisibleText(entry.block)) errors.push(`${base}.block: invalid text`);
    if (!isVisibleText(entry.reason)) errors.push(`${base}.reason: invalid text`);
    if (typeof entry.block === "string" && seen.has(entry.block)) errors.push(`${base}.block: duplicate entry`);
    if (typeof entry.block === "string") seen.add(entry.block);
  });
}

function semanticKey(unit) {
  return JSON.stringify([
    unit.surface,
    unit.boundary,
    unit.subsystem,
    unit.attack_class,
    hasOwn(unit, "lifecycle") ? unit.lifecycle : null,
  ]);
}

function hasValidSemanticFields(unit) {
  return ["surface", "boundary", "subsystem", "attack_class"].every((field) => isVisibleText(unit[field])) &&
    (!hasOwn(unit, "lifecycle") || isVisibleText(unit.lifecycle));
}

function requireEmptyArray(unit, field, base, errors) {
  if (Array.isArray(unit[field]) && unit[field].length > 0) {
    errors.push(`${base}.${field}: unit with status ${safeQuote(unit.status)} must keep this array empty`);
  }
}

function requireNonemptyArray(unit, field, base, errors) {
  if (!Array.isArray(unit[field]) || unit[field].length === 0) {
    errors.push(`${base}.${field}: unit with status ${safeQuote(unit.status)} requires entries`);
  }
}

function validateStateInvariants(unit, base, errors) {
  const emptyEvidence = () => {
    requireEmptyArray(unit, "reviewed_paths", base, errors);
    requireEmptyArray(unit, "local_checks", base, errors);
  };
  const requireOwner = () => {
    if (!isSafeAgentId(unit.agent_id)) errors.push(`${base}.agent_id: unit with status ${safeQuote(unit.status)} requires a canonical lowercase agent ID`);
  };

  if (unit.status !== "candidate") requireEmptyArray(unit, "result_fingerprints", base, errors);

  switch (unit.status) {
    case "planned":
      if (unit.agent_id !== null) errors.push(`${base}.agent_id: planned unit must be unassigned`);
      emptyEvidence();
      requireEmptyArray(unit, "unresolved", base, errors);
      break;
    case "not_applicable":
    case "out_of_scope":
    case "deferred":
      if (unit.agent_id !== null) errors.push(`${base}.agent_id: unit with status ${safeQuote(unit.status)} must be unassigned`);
      emptyEvidence();
      requireNonemptyArray(unit, "unresolved", base, errors);
      break;
    case "in_progress":
      requireOwner();
      emptyEvidence();
      requireEmptyArray(unit, "unresolved", base, errors);
      break;
    case "blocked":
      requireOwner();
      requireNonemptyArray(unit, "reviewed_paths", base, errors);
      requireNonemptyArray(unit, "local_checks", base, errors);
      requireNonemptyArray(unit, "unresolved", base, errors);
      break;
    case "covered":
      requireOwner();
      requireNonemptyArray(unit, "reviewed_paths", base, errors);
      requireNonemptyArray(unit, "local_checks", base, errors);
      requireEmptyArray(unit, "unresolved", base, errors);
      break;
    case "candidate":
      requireOwner();
      requireNonemptyArray(unit, "reviewed_paths", base, errors);
      requireNonemptyArray(unit, "local_checks", base, errors);
      requireNonemptyArray(unit, "result_fingerprints", base, errors);
      break;
  }
}

function validateAttempts(value, unit, base, errors) {
  if (!Array.isArray(value)) {
    errors.push(`${base}.attempts: expected array`);
    return;
  }
  if (value.length > MAX_LIST_ITEMS) errors.push(`${base}.attempts: exceeds ${MAX_LIST_ITEMS} entries`);

  const priorOwners = new Set();
  const priorArtifacts = new Set();
  let previousWave = 0;
  value.slice(0, MAX_LIST_ITEMS).forEach((attempt, index) => {
    const attemptBase = `${base}.attempts[${index}]`;
    if (!isObject(attempt)) {
      errors.push(`${attemptBase}: expected object`);
      return;
    }
    for (const field of ATTEMPT_FIELDS) {
      if (!hasOwn(attempt, field)) errors.push(`${attemptBase}: missing required field ${safeQuote(field)}`);
    }
    if (!Number.isInteger(attempt.wave) || attempt.wave < 1) {
      errors.push(`${attemptBase}.wave: expected a positive integer`);
    } else {
      if (attempt.wave <= previousWave) errors.push(`${attemptBase}.wave: archived attempt waves must be strictly increasing`);
      if (Number.isInteger(unit.wave) && attempt.wave >= unit.wave) {
        errors.push(`${attemptBase}.wave: archived attempt wave must precede current wave ${safeQuote(unit.wave)}`);
      }
      previousWave = attempt.wave;
    }
    if (!ATTEMPT_STATUSES.has(attempt.status)) {
      errors.push(`${attemptBase}.status: expected "covered", "candidate", or "blocked"`);
    }
    let hasFreshOwner = false;
    if (!isSafeAgentId(attempt.agent_id)) {
      errors.push(`${attemptBase}.agent_id: archived attempt requires a canonical lowercase agent ID`);
    } else if (priorOwners.has(attempt.agent_id)) {
      errors.push(`${attemptBase}.agent_id: assignment owner must be fresh for each attempt`);
    } else {
      hasFreshOwner = true;
    }
    validateStringArray(attempt.reviewed_paths, `${attemptBase}.reviewed_paths`, errors, { pathValue: true });
    validateChecks(attempt.local_checks, `${attemptBase}.local_checks`, errors);
    validateReviewedPathOwnership(attempt, attemptBase, errors);
    validateStringArray(attempt.result_fingerprints, `${attemptBase}.result_fingerprints`, errors, { fingerprint: true });
    validateStringArray(attempt.unresolved, `${attemptBase}.unresolved`, errors);
    if (!isVisibleText(attempt.reassignment_reason)) errors.push(`${attemptBase}.reassignment_reason: invalid text`);
    validateStateInvariants(attempt, attemptBase, errors);

    const attemptArtifacts = new Set();
    if (Array.isArray(attempt.local_checks)) {
      attempt.local_checks.forEach((check, checkIndex) => {
        if (!isObject(check)) return;
        if (priorOwners.has(check.agent_id)) {
          errors.push(`${attemptBase}.local_checks[${checkIndex}].agent_id: prior assignment owner evidence must remain in its earlier attempt`);
        }
        if (typeof check.artifact === "string" && priorArtifacts.has(check.artifact)) {
          errors.push(`${attemptBase}.local_checks[${checkIndex}].artifact: artifact from an earlier attempt cannot be reused`);
        }
        if (check.method === "local" && typeof check.artifact === "string") attemptArtifacts.add(check.artifact);
      });
    }
    for (const artifact of attemptArtifacts) priorArtifacts.add(artifact);
    if (hasFreshOwner) priorOwners.add(attempt.agent_id);
  });

  if (isSafeAgentId(unit.agent_id) && priorOwners.has(unit.agent_id)) {
    errors.push(`${base}.agent_id: current assignment owner must be fresh after reassignment`);
  }
  if (Array.isArray(unit.local_checks)) {
    unit.local_checks.forEach((check, index) => {
      if (!isObject(check)) return;
      if (priorOwners.has(check.agent_id)) {
        errors.push(`${base}.local_checks[${index}].agent_id: prior assignment owner evidence must remain in its archived attempt`);
      }
      if (typeof check.artifact === "string" && priorArtifacts.has(check.artifact)) {
        errors.push(`${base}.local_checks[${index}].artifact: artifact from an archived attempt cannot be reused`);
      }
    });
  }
}

function collectUnitErrors(unit, index) {
  const errors = createErrorList();
  const base = `$[${index}]`;
  if (!isObject(unit)) {
    errors.push(`${base}: expected object`);
    return errors;
  }

  for (const field of REQUIRED_FIELDS) {
    if (!hasOwn(unit, field)) errors.push(`${base}: missing required field ${safeQuote(field)}`);
  }
  for (const field of ["surface", "boundary", "subsystem", "attack_class"]) {
    if (!isVisibleText(unit[field])) errors.push(`${base}.${field}: invalid text`);
  }
  if (hasOwn(unit, "lifecycle") && !isVisibleText(unit.lifecycle)) errors.push(`${base}.lifecycle: invalid text`);

  let expectedId = null;
  if (!isObject(unit.canonical_refs)) {
    errors.push(`${base}.canonical_refs: expected object`);
  } else {
    const expectedFields = hasOwn(unit.canonical_refs, "lifecycle") ? [...REF_FIELDS, "lifecycle"] : REF_FIELDS;
    for (const field of expectedFields) {
      if (!hasOwn(unit.canonical_refs, field)) {
        errors.push(`${base}.canonical_refs: missing required field ${safeQuote(field)}`);
      } else if (!isCanonicalRef(unit.canonical_refs[field])) {
        errors.push(`${base}.canonical_refs.${field}: invalid canonical reference`);
      }
    }
    if (Object.keys(unit.canonical_refs).some((field) => !expectedFields.includes(field))) {
      errors.push(`${base}.canonical_refs: contains unexpected fields`);
    }
    if (hasOwn(unit, "lifecycle") !== hasOwn(unit.canonical_refs, "lifecycle")) {
      errors.push(`${base}: lifecycle and canonical_refs.lifecycle must appear together`);
    }
    try {
      expectedId = canonicalCoverageId(unit.canonical_refs);
    } catch {
      // The specific reference errors above are more useful.
    }
  }
  if (!isVisibleText(unit.coverage_id, 65536)) {
    errors.push(`${base}.coverage_id: invalid text`);
  } else if (expectedId !== null && unit.coverage_id !== expectedId) {
    errors.push(`${base}.coverage_id: expected canonical ID ${safeQuote(expectedId)}`);
  }

  validateStringArray(unit.starting_paths, `${base}.starting_paths`, errors, { allowEmpty: false, pathValue: true });
  if (unit.ordinary_attack_class_block !== null && !isVisibleText(unit.ordinary_attack_class_block)) {
    errors.push(`${base}.ordinary_attack_class_block: expected null or non-empty text`);
  }
  validateStringArray(unit.selected_companion_blocks, `${base}.selected_companion_blocks`, errors);
  validateExcludedBlocks(unit.excluded_blocks, `${base}.excluded_blocks`, errors);
  if (Array.isArray(unit.selected_companion_blocks) && Array.isArray(unit.excluded_blocks)) {
    const selected = new Set(unit.selected_companion_blocks);
    unit.excluded_blocks.forEach((entry, blockIndex) => {
      if (isObject(entry) && selected.has(entry.block)) {
        errors.push(`${base}.excluded_blocks[${blockIndex}].block: block is also selected`);
      }
    });
  }

  if (!PRIOR_STATUSES.has(unit.prior_status)) errors.push(`${base}.prior_status: invalid value ${safeQuote(unit.prior_status)}`);
  if (!STATUSES.has(unit.status)) errors.push(`${base}.status: invalid value ${safeQuote(unit.status)}`);
  if (!Number.isInteger(unit.wave) || unit.wave < 1) errors.push(`${base}.wave: expected a positive integer`);
  if (unit.agent_id !== null && !isSafeAgentId(unit.agent_id)) errors.push(`${base}.agent_id: expected null or a safe agent ID`);

  validateAttempts(unit.attempts, unit, base, errors);
  validateStringArray(unit.reviewed_paths, `${base}.reviewed_paths`, errors, { pathValue: true });
  validateChecks(unit.local_checks, `${base}.local_checks`, errors);
  validateReviewedPathOwnership(unit, base, errors);
  validateStringArray(unit.result_fingerprints, `${base}.result_fingerprints`, errors, { fingerprint: true });
  validateStringArray(unit.unresolved, `${base}.unresolved`, errors);

  validateStateInvariants(unit, base, errors);

  return errors;
}

function validateDocument(ledger) {
  const errors = createErrorList();
  if (!Array.isArray(ledger)) {
    errors.push("$: expected a top-level array");
    return errors;
  }
  if (ledger.length > MAX_UNITS) {
    errors.push(`$: exceeds ${MAX_UNITS} coverage units`);
    return errors;
  }

  const preflightErrors = preflightDocument(ledger);
  appendErrors(errors, preflightErrors);
  if (errors.length > 0) return errors;

  const ids = new Map();
  const semantics = new Map();
  let previousId = null;
  for (let index = 0; index < ledger.length && !errors.truncated; index++) {
    const unit = ledger[index];
    const unitErrors = collectUnitErrors(unit, index);
    appendErrors(errors, unitErrors);
    if (!isObject(unit) || typeof unit.coverage_id !== "string") continue;

    const key = hasValidSemanticFields(unit) ? semanticKey(unit) : null;
    if (ids.has(unit.coverage_id)) {
      const previous = ids.get(unit.coverage_id);
      const qualifier = key !== null && previous.key !== null && previous.key !== key
        ? "canonical identity collision with different semantic fields"
        : "duplicate coverage ID";
      errors.push(`$[${index}].coverage_id: ${qualifier} at $[${previous.index}]`);
    } else {
      ids.set(unit.coverage_id, { index, key });
    }
    if (key !== null && semantics.has(key) && semantics.get(key).id !== unit.coverage_id) {
      const previous = semantics.get(key);
      errors.push(`$[${index}].canonical_refs: semantic tuple already uses coverage ID ${safeQuote(previous.id)} at $[${previous.index}]`);
    } else if (key !== null) {
      semantics.set(key, { id: unit.coverage_id, index });
    }
    if (previousId !== null && previousId > unit.coverage_id) {
      errors.push(`$[${index}].coverage_id: units must be sorted lexicographically`);
    }
    previousId = unit.coverage_id;
  }
  return errors;
}

function run(file) {
  if (!file) {
    console.error("Usage: node validate-coverage-ledger.cjs <path-to-coverage-ledger.json>");
    return 1;
  }

  let contents;
  try {
    contents = readFileWithinLimit(file);
  } catch (error) {
    const reason = error instanceof SafeInputError ? error.message : "input could not be opened or read safely";
    console.error(`Failed to read coverage ledger: ${reason}`);
    return 1;
  }

  let ledger;
  try {
    preflightJsonText(contents);
  } catch (error) {
    const reason = error instanceof JsonStructureError ? error.message : "invalid JSON structure";
    console.error(`Failed to parse coverage ledger: ${reason}`);
    return 1;
  }
  try {
    ledger = JSON.parse(contents);
  } catch {
    console.error("Failed to parse coverage ledger: invalid JSON syntax");
    return 1;
  }

  let errors;
  try {
    errors = validateDocument(ledger);
  } catch {
    console.error("Failed to validate coverage ledger: unexpected validation error");
    return 1;
  }
  for (const message of errors) console.error("ERROR:", message);
  if (errors.length > 0) {
    const cap = errors.truncated ? `; output capped at ${LIMITS.validationErrors}` : "";
    console.error(`FAIL: ${errors.length} validation error(s)${cap}`);
    return 1;
  }
  console.log(`PASS: ${ledger.length} coverage units valid`);
  return 0;
}

module.exports = {
  LIMITS,
  PATH_FORBIDDEN_CHARACTER,
  UNSAFE_DIAGNOSTIC_CHARACTER,
  VISIBLE_CONTENT,
  WINDOWS_RESERVED_COMPONENT,
  canonicalCoverageId,
  createErrorList,
  encodeCanonicalRef,
  escapeUnsafeDiagnosticCharacters,
  hasVisibleProse,
  isSafeAgentId,
  isSafeRelativePath,
  preflightJsonText,
  readFileWithinLimit,
  safeQuote,
  validateDocument,
};

if (require.main === module) process.exit(run(process.argv[2]));
