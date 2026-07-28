import {
  createCipheriv,
  createHmac,
  generateKeyPairSync,
  randomBytes,
  sign,
} from "node:crypto";
import {
  lstatSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import {
  CARD_RELATIVE,
  PUBLIC_KEY_RELATIVE,
  SIGNATURE_RELATIVE,
  assertKeyCorrespondence,
  fixedExistingFile,
  fixedNewFile,
  publicFingerprint,
  readCanonicalCard,
  testPaths,
  validateCardSchema,
  verifyRepositoryAssertions,
} from "./change_card_common.mjs";

function exclusiveWrite(path, bytes, mode = 0o600) {
  writeFileSync(path, bytes, { flag: "wx", mode });
  const status = lstatSync(path);
  if (!status.isFile() || status.isSymbolicLink()) throw new Error("OUTPUT_NOT_REGULAR");
}

function generatePair() {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return {
    privatePem: privateKey.export({ type: "pkcs8", format: "pem" }),
    publicPem: publicKey.export({ type: "spki", format: "pem" }),
  };
}

function generateWrapped(transportHex) {
  if (!/^[0-9a-f]{64}$/.test(transportHex)) {
    throw new Error("TRANSPORT_KEY_INVALID");
  }
  const transport = Buffer.from(transportHex, "hex");
  const pair = generatePair();
  const privateBytes = Buffer.from(pair.privatePem, "utf8");
  const iv = randomBytes(16);
  try {
    const cipher = createCipheriv("aes-256-cbc", transport, iv);
    const encrypted = Buffer.concat([cipher.update(privateBytes), cipher.final()]);
    const fingerprint = publicFingerprint(pair.publicPem);
    const tag = createHmac("sha256", transport)
      .update(iv)
      .update(encrypted)
      .update(Buffer.from(pair.publicPem, "utf8"))
      .update(Buffer.from(fingerprint, "ascii"))
      .digest();
    process.stdout.write(JSON.stringify({
      encryptedPrivateKey: encrypted.toString("base64"),
      fingerprint,
      iv: iv.toString("base64"),
      publicKey: pair.publicPem,
      tag: tag.toString("base64"),
    }));
    encrypted.fill(0);
    tag.fill(0);
  } finally {
    privateBytes.fill(0);
    transport.fill(0);
    iv.fill(0);
  }
}

function checkPair() {
  const input = readFileSync(0);
  try {
    const value = JSON.parse(input.toString("utf8"));
    if (!value || Object.keys(value).sort().join(",") !==
        "privateKey,publicKey" ||
        typeof value.privateKey !== "string" ||
        typeof value.publicKey !== "string") throw new Error("PAIR_INPUT_INVALID");
    assertKeyCorrespondence(value.privateKey, value.publicKey);
    process.stdout.write(JSON.stringify({
      fingerprint: publicFingerprint(value.publicKey),
      status: "PAIR_VERIFIED",
    }));
  } finally {
    input.fill(0);
  }
}

function ephemeralMismatchedPairCheck() {
  const first = generatePair();
  const second = generatePair();
  try {
    let rejected = false;
    try {
      assertKeyCorrespondence(first.privatePem, second.publicPem);
    } catch (error) {
      if (error instanceof Error && error.message === "KEY_PAIR_MISMATCH") {
        rejected = true;
      } else {
        throw error;
      }
    }
    if (!rejected) throw new Error("MISMATCHED_PAIR_ACCEPTED");
    process.stdout.write(JSON.stringify({ status: "MISMATCH_REJECTED" }));
  } finally {
    Buffer.from(first.privatePem, "utf8").fill(0);
    Buffer.from(second.privatePem, "utf8").fill(0);
  }
}

function productionSign() {
  const cardPath = fixedExistingFile(CARD_RELATIVE);
  const publicPath = fixedExistingFile(PUBLIC_KEY_RELATIVE);
  const signaturePath = fixedNewFile(SIGNATURE_RELATIVE);
  const { card, bytes } = readCanonicalCard(cardPath);
  verifyRepositoryAssertions(card);
  const publicPem = readFileSync(publicPath, "utf8");
  const privateBytes = readFileSync(0);
  try {
    const privateKey = assertKeyCorrespondence(
      privateBytes.toString("utf8"),
      publicPem,
    );
    const signature = sign(null, bytes, privateKey);
    if (signature.length !== 64) throw new Error("SIGNATURE_LENGTH_INVALID");
    exclusiveWrite(signaturePath, signature.toString("base64"));
    process.stdout.write(JSON.stringify({
      fingerprint: publicFingerprint(publicPem),
      status: "SIGNED",
    }));
  } finally {
    privateBytes.fill(0);
  }
}

function ephemeralTestSign(testId) {
  const paths = testPaths(testId, { requireExisting: true });
  const { card, bytes } = readCanonicalCard(paths.card);
  validateCardSchema(card);
  const pair = generatePair();
  try {
    exclusiveWrite(paths.publicKey, pair.publicPem, 0o644);
    const signature = sign(null, bytes, pair.privatePem);
    exclusiveWrite(paths.signature, signature.toString("base64"));
    process.stdout.write(JSON.stringify({
      fingerprint: publicFingerprint(pair.publicPem),
      status: "TEST_SIGNED",
    }));
  } finally {
    Buffer.from(pair.privatePem, "utf8").fill(0);
  }
}

const [mode, argument] = process.argv.slice(2);
if (mode === "--sign" && argument === undefined) {
  productionSign();
} else if (mode === "--ephemeral-test-sign" && argument) {
  ephemeralTestSign(argument);
} else if (mode === "--generate-wrapped" && argument === undefined) {
  const transportInput = readFileSync(0);
  try {
    generateWrapped(transportInput.toString("ascii").trim());
  } finally {
    transportInput.fill(0);
  }
} else if (mode === "--check-pair" && argument === undefined) {
  checkPair();
} else if (mode === "--ephemeral-test-mismatched-pair" &&
           argument === undefined) {
  ephemeralMismatchedPairCheck();
} else {
  throw new Error("USAGE_ERROR");
}
